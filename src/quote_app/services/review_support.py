"""Local post-generation decision and audit service; no collection side effects."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from calendar import monthrange
from decimal import Decimal, localcontext
from pathlib import Path
import hashlib
import html
import json
from collections import Counter
from quote_app.domain.models import QuoteMonth, QuoteRow, Issue
from quote_app.desktop_state import TaskRow, DesktopState
from quote_app.services.review_workbook import (
    COLUMNS,
    digest,
    money,
    inspect,
    write_cells,
    atomic_json,
    rollback,
)
from quote_app.services.review_sources import warehouse_limit, workbook_channels, matching_run_paths, source_facts, recover_task_provenance
from quote_app.services.screenshot_review import request_scan, assess

CATEGORIES = (
    "商品分类与关联",
    "渠道与价格口径",
    "截图内容核验",
    "证据与数据完整性",
    "报价规则与资格",
    "报表与报送一致性",
)
RULE_VERSION = "decision-audit-2026-09-13-v2"


@dataclass(slots=True)
class ReviewCheck:
    id: str
    product_id: str
    code: str
    category: str
    title: str
    status: str
    comparison: str = ""
    reason: str = ""
    evidence_paths: tuple[Path, ...] = ()
    human_reviewable: bool = False


@dataclass(slots=True)
class ReviewProduct:
    id: str
    title: str
    specification: str
    material_code: str
    output_row: int
    values: dict[str, str] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)
    attachments: list[Path] = field(default_factory=list)
    channels: list[TaskRow] = field(default_factory=list)


def can_confirm(checks):
    return bool(checks) and all(
        c.status == "通过" or (c.status == "不适用" and c.reason.strip()) for c in checks
    )


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _anniversary(start, months):
    count = start.year * 12 + start.month - 1 + months
    year, month = divmod(count, 12)
    month += 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def _phone_limit(value):
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = max(28, len(value.as_tuple().digits) + 5)
        return value * Decimal("1.045")


def price_ceiling(product):
    """Only known numeric bounds, never an overall eligibility conclusion."""
    candidates = []
    if product.context.get("category") == "手机" and money(product.values.get("L")):
        candidates.append((_phone_limit(money(product.values["L"])), "手机采购价×1.045"))
    for value, label in (
        (product.values.get("Q"), "分销零售价"),
        (warehouse_limit(product.context.get("warehouse_price")), "一级库建议零售价上限"),
        (product.context.get("previous_price"), "已知上期报价（不代表历史全量）"),
    ):
        if money(value):
            candidates.append((money(value), label))
    for period, value in supplied_history(product):
        candidates.append((value, f'{period}已提供历史报价'))
    # Unreviewed raw channel observations are not valid price ceilings.
    if not candidates:
        return None, "尚无可计算约束；渠道有效性及其他资格仍须核验"
    low = min(v for v, _ in candidates)
    return low, "、".join(
        label for v, label in candidates if v == low
    ) + "；其余资格与价格口径仍须核验"


def supplied_history(product):
    try:
        rows = json.loads(product.context.get('history_prices', '[]'))
        return [(str(period), money(value)) for period, value in rows if money(value) is not None]
    except (ValueError, TypeError):
        return []


class ReviewSession:
    def __init__(self, model, month: QuoteMonth, *, _output_rows=None, _source_metadata=True, source_paths=None):
        self.model = model
        self.month = month
        self.products = []
        self.quote_path = Path(model.quote_path) if model.quote_path else None
        self._version = digest(self.quote_path, fresh=True)
        self._rows = {}
        self._identities = {}
        self._sources = {}
        self._source_known = {}
        self._reviews = {}
        self._history = []
        self._confirmed = {}
        self.load_error = ""
        self._inspection_error = ""
        self.source_paths = dict(source_paths or {})
        try:
            self._rows = inspect(self.quote_path, month)
        except ValueError as error:
            self.load_error = str(error)
            self._inspection_error = str(error)
        self.batch_id = _hash(
            (str(self.quote_path.resolve()) if self.quote_path else "", month.year, month.month)
        )[:24]
        self.store_path = (
            self.quote_path.with_name(self.quote_path.name + ".review.json")
            if self.quote_path
            else None
        )
        for index, row in enumerate(model.quote_rows):
            output_row = _output_rows[index] if _output_rows is not None else index + 2
            saved = self._rows.get(output_row, {})
            ident = _hash(
                (
                    self.batch_id,
                    output_row,
                    [(k, str(saved.get(k, "") or "")) for k in ("C", "D", "E", "F", "G")],
                )
            )[:24]
            query = row.web_query
            specification = " / ".join(
                str(v) for v in (query.ram, query.storage, query.color) if v
            ) or str(row.cells.get("E", ""))
            context = {
                "category": "手机" if not row.cells.get("A") or "手机" in str(row.cells.get("A")) else "暂不支持",
                "stock": "未确认",
                "entry_date": "",
                "first_quote_date": "",
                "youfu": "未确认",
                "qualification_note": "",
                "warehouse_price": str(saved.get("O", row.cells.get("O", "")) or ""),
                "previous_price": str(saved.get("J", row.cells.get("J", "")) or ""),
            }
            entry = saved.get("H", row.cells.get("H"))
            if isinstance(entry, (date, datetime)):
                context["entry_date"] = (
                    entry.date().isoformat() if isinstance(entry, datetime) else entry.isoformat()
                )
            elif _date(entry):
                context["entry_date"] = _date(entry).isoformat()
            p = ReviewProduct(
                ident,
                str(
                    query.model_name
                    or row.cells.get("D")
                    or row.cells.get("E")
                    or row.material_code
                ),
                specification,
                row.material_code,
                output_row,
                {c: str(saved.get(c, row.cells.get(c, "")) or "") for c in COLUMNS},
                context,
            )
            p.channels = [
                t
                for t in model.rows
                if getattr(t, "source_row_number", 0) == row.source_row_number
                and getattr(t, "material_code", "") == row.material_code
            ]
            self.products.append(p)
            self._sources[p.id] = row
            self._source_known[p.id] = _source_metadata
            self._identities[p.id] = tuple(
                str((row.material_code if c == "C" else row.cells.get(c, "")) or "")
                for c in ("C", "D", "E", "F", "G")
            )
        self._inspection_version = self._version
        self._restore()
        for p in self.products:
            # Source facts override older manual placeholders; no invented dates.
            p.context.update(source_facts(self.source_paths, p.material_code, self.month))
            if p.context.get('category') == '未确认':
                source_category = str(self._sources[p.id].cells.get('A') or '')
                p.context['category'] = '手机' if not source_category or '手机' in source_category else '暂不支持'
            if p.context.get('source_title'):
                p.title = p.context['source_title']
                p.specification = ' / '.join(p.context.get(k, '') for k in ('source_ram', 'source_storage', 'source_color') if p.context.get(k))
            elif not _source_metadata:
                p.title = str(self._sources[p.id].cells.get('E') or p.title)

    @classmethod
    def from_workbook(cls, path: Path, month: QuoteMonth, *, source_paths=None, task_database=None):
        rows = inspect(Path(path), month)
        source = []
        for number, cells in rows.items():
            if cells.get("C"):
                from quote_app.domain.models import WebQuery
                source.append(QuoteRow(number, str(cells["C"]), dict(cells), web_query=WebQuery(
                    model_name=str(cells.get('E') or cells.get('D') or cells['C']), brand=str(cells.get('B') or ''))))
            elif any(cells.get(c) for c in ("D", "E", "K", "L")):
                raise ValueError(f"工作簿第{number}行缺少稳定物料编码，无法安全定位")
        if not source:
            raise ValueError("工作簿中没有可报价商品记录")
        run_ids = []
        paths = matching_run_paths(path, month, rows, task_database, run_ids=run_ids)
        paths.update({k: Path(v) for k, v in (source_paths or {}).items() if v and Path(v).is_file() and k not in paths})
        result = cls(
            DesktopState(quote_rows=tuple(source), quote_path=Path(path)),
            month,
            _output_rows=[row.source_row_number for row in source],
            _source_metadata=False,
            source_paths=paths,
        )
        recovered = recover_task_provenance(workbook_channels(path, rows), task_database,
                                            run_id=run_ids[0] if run_ids else None)
        for product in result.products:
            # Existing saved task provenance is stronger than workbook-only observations.
            for channel in recovered:
                if channel.source_row_number != product.output_row or channel.material_code != product.material_code:
                    continue
                old = next((c for c in product.channels if c.channel == channel.channel), None)
                if old is None:
                    product.channels.append(channel)
                elif channel.state != 'workbook' or not old.evidence_path or not old.evidence_path.is_file():
                    product.channels[product.channels.index(old)] = channel
        return result

    @property
    def running(self):
        return bool(self.model.running)

    def _restore(self):
        if self.store_path is None or not self.store_path.is_file():
            return
        try:
            data = json.loads(self.store_path.read_text())
            if data.get("batch_id") != self.batch_id:
                raise ValueError("复核记录批次不匹配")
            self._history = data.get("history", [])
            if data.get("workbook_version") == self._version:
                self._reviews = data.get("reviews", {})
                self._confirmed = data.get("confirmed", {})
                for p in self.products:
                    saved = data.get("products", {}).get(p.id, {})
                    if not self._source_known[p.id]:
                        metadata = saved.get("source", {})
                        source = self._sources[p.id]
                        expected = {
                            c: str(source.cells.get(c, "") or "") for c in ("D", "E", "F", "G")
                        }
                        if (
                            metadata.get("available") is True
                            and metadata.get("material_code") == p.material_code
                            and metadata.get("association_cells") == expected
                            and isinstance(metadata.get("issues"), list)
                        ):
                            source.issues = [Issue(**issue) for issue in metadata["issues"]]
                            source.source_row_number = int(metadata["source_row_number"])
                            self._source_known[p.id] = True
                    # J is a workbook source value, never a manual qualification.
                    p.context.update(
                        {
                            key: value
                            for key, value in saved.get("context", {}).items()
                            if key != "previous_price"
                        }
                    )
                    p.attachments = [Path(v) for v in saved.get("attachments", [])]
                    if not p.channels:
                        for record in saved.get("channels", []):
                            record = dict(record)
                            record["evidence_path"] = (
                                Path(record["evidence_path"])
                                if record.get("evidence_path")
                                else None
                            )
                            p.channels.append(TaskRow(**record))
                    if saved.get("title"):
                        p.title = saved["title"]
                    if saved.get("specification"):
                        p.specification = saved["specification"]
            else:
                self.load_error = "工作簿版本已变化，旧人工依据与复核失效，请重新补充核验"
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.load_error = f"本地复核记录读取失败：{error}"

    def _persist(self):
        if self.store_path is None:
            raise ValueError("尚无报价输出路径，无法保存本地复核记录")
        data = {
            "batch_id": self.batch_id,
            "rule_version": RULE_VERSION,
            "workbook_version": self._version,
            "updated_at": _now(),
            "month": asdict(self.month),
            "products": {
                p.id: {
                    "title": p.title,
                    "specification": p.specification,
                    "source_row_number": self._sources[p.id].source_row_number,
                    "source": {
                        "available": self._source_known[p.id],
                        "source_row_number": self._sources[p.id].source_row_number,
                        "material_code": p.material_code,
                        "association_cells": {
                            c: str(self._sources[p.id].cells.get(c, "") or "")
                            for c in ("D", "E", "F", "G")
                        },
                        "issues": [asdict(issue) for issue in self._sources[p.id].issues],
                    },
                    "channels": [
                        {
                            **asdict(t),
                            "evidence_path": str(t.evidence_path) if t.evidence_path else None,
                        }
                        for t in p.channels
                    ],
                    "context": p.context,
                    "values": p.values,
                    "attachments": [str(a) for a in p.attachments],
                }
                for p in self.products
            },
            "reviews": self._reviews,
            "history": self._history,
            "confirmed": self._confirmed,
        }
        atomic_json(self.store_path, json.dumps(data, ensure_ascii=False, indent=2))

    def _fingerprint(self, p):
        return _hash(
            (
                RULE_VERSION,
                p.id,
                p.title,
                p.specification,
                p.material_code,
                p.output_row,
                p.values,
                p.context,
                self._source_known[p.id],
                [asdict(issue) for issue in self._sources[p.id].issues],
                [(str(a), digest(a)) for a in p.attachments],
                [{**asdict(t), "file_version": digest(t.evidence_path)} for t in p.channels],
                digest(self.quote_path),
            )
        )

    def evaluate(self, p):
        if not any(item is p for item in self.products):
            raise ValueError("商品不属于当前批次")
        source_row = self._sources[p.id].source_row_number
        current = [t for t in self.model.rows if t.source_row_number == source_row
                   and t.material_code == p.material_code]
        if current:
            p.channels = current
        checks = []
        fingerprint = self._fingerprint(p)

        def add(code, title, status, comparison="", reason="", paths=(), human=False, suffix=""):
            c = ReviewCheck(
                f"{self.batch_id}:{p.id}:{code}:{suffix}",
                p.id,
                code,
                CATEGORIES[ord(code[0]) - 65],
                title,
                status,
                str(comparison),
                reason,
                tuple(paths),
                human,
            )
            record = self._reviews.get(c.id)
            if human and status == "待复核" and record and record["version"] == fingerprint:
                c.status = record["conclusion"]
                c.reason += (
                    f"；人工复核：{record['reason']}（{record['operator']}，{record['time']}）"
                )
            checks.append(c)
            return c

        source = self._sources[p.id]
        category = p.context.get("category", "未确认")
        note = p.context.get("qualification_note", "").strip()
        add(
            "A01",
            "商品分类",
            "通过" if category == "手机" else "待补充",
            category,
            "当前智能报价仅适用于手机；按手机4.5%规则计算" if category == "手机" else "当前版本暂不支持非手机产品智能报价",
        )
        add(
            "A02",
            "基础与资源规格一致",
            "待复核",
            p.specification,
            "请核对基础表、营销商品及资源规格",
            human=bool(p.specification),
        )
        association_issues = [i for i in source.issues if "MARKETING" in i.code or "BOP" in i.code]
        add(
            "A03",
            "业务记录关联",
            "待补充"
            if association_issues or not p.material_code or not self._source_known[p.id]
            else "待复核",
            p.material_code,
            "；".join(i.message for i in association_issues)
            or (
                "需核对业务编码与资源关联依据"
                if self._source_known[p.id]
                else "缺少原始关联检查记录，请补充来源与冲突检查依据"
            ),
            human=self._source_known[p.id] and not association_issues and bool(p.material_code),
        )
        duplicate = (
            sum(
                x.material_code == p.material_code and x.specification == p.specification
                for x in self.products
            )
            > 1
        )
        add(
            "A04",
            "重复与漏项",
            "未通过" if duplicate else "待复核",
            f"本批次{len(self.products)}条商品",
            "存在重复编码与规格" if duplicate else "未见同编码同规格重复；源表完整范围仍需核对",
            human=not duplicate,
        )
        add(
            "D01",
            "应有渠道记录",
            "未检查" if self.running else ("通过" if {t.channel for t in p.channels} == {'jd', 'tmall', 'official'} else "待补充"),
            f"{len(p.channels)}条渠道记录",
            "检查官网、天猫、京东三个渠道记录是否齐全；取价失败和无机型的业务结果另列核验",
            human=False,
        )
        eligible = []
        for t in p.channels:
            suffix = t.task_id
            channel_label = {'jd': '京东', 'tmall': '天猫', 'official': '官网'}.get(t.channel, t.channel)
            paths = (t.evidence_path,) if t.evidence_path else ()
            record_ok = (
                t.state == "succeeded" and t.outcome == "price_found" and money(t.price) is not None
            )
            for code, title, comparison, reason in (
                ("B01", "渠道与店铺", t.channel + " " + t.url, "核对是否规定官方来源"),
                ("B02", "报价对象", t.specification or p.specification, "核对可售状态和目标配置"),
                ("B03", "优惠条件", t.price, "优惠接受口径未定义，不能直接认定有效"),
            ):
                add(
                    code,
                    title,
                    "待复核" if t.evidence_path else "待补充",
                    comparison,
                    reason,
                    paths,
                    human=bool(t.evidence_path) and code != "B03",
                    suffix=suffix,
                )
            file_ok = False
            if t.evidence_path and t.evidence_path.is_file():
                try:
                    from PIL import Image

                    with Image.open(t.evidence_path) as im:
                        im.verify()
                    file_ok = t.evidence_path.stat().st_size > 0
                except (OSError, ValueError):
                    pass
            add(
                "D02",
                "截图文件有效",
                "通过" if file_ok else "待补充",
                str(t.evidence_path or ""),
                "文件可读取；不代表内容已通过" if file_ok else "截图缺失、空文件或无法解码",
                paths,
                suffix=suffix,
            )
            content = assess(request_scan(t.evidence_path), p.title,
                             t.specification or p.specification, t.channel, t.price,
                             outcome=t.outcome) if file_ok else {}
            for code, title in (
                ("C01", "截图页面内容"),
                ("C02", "截图规格对照"),
                ("C03", "截图价格与店铺"),
                ("C04", "截图可读与完整"),
            ):
                add(
                    code,
                    title,
                    content[code][0] if file_ok else "待补充",
                    channel_label + ' · ' + (content[code][1] if file_ok else "没有可读取的截图"),
                    content[code][2] if file_ok else (
                        "本次取价未完成：仅识别到补贴价或划线原价，未取得符合规则的售价与截图"
                        if t.error == 'NO_VALID_SELLING_PRICE' else
                        f"截图缺失、空文件或无法解码，请补充证据{('；任务原因：' + t.error) if t.error else ''}"),
                    paths,
                    human=file_ok and content[code][0] == '待复核',
                    suffix=suffix,
                )
            reused = (
                t.evidence_path
                and sum(
                    x.evidence_path == t.evidence_path
                    for item in self.products
                    for x in item.channels
                )
                > 1
            )
            add(
                "D03",
                "证据关联完整",
                "未通过" if reused else ("通过" if file_ok and t.material_code == p.material_code and t.source_row_number else "待补充"),
                t.task_id,
                "同一截图被多条记录重复引用" if reused else "截图按商品物料编码、数据行和渠道关联；内容及价格口径另项核验",
                paths,
                human=file_ok and bool(t.url) and not reused,
                suffix=suffix,
            )
            if record_ok:
                eligible.append(t)
        if not p.channels:
            for code, title in (
                ("B01", "渠道与店铺"),
                ("B02", "报价对象"),
                ("B03", "优惠条件"),
                ("C01", "截图页面内容"),
                ("C02", "截图规格对照"),
                ("C03", "截图价格与店铺"),
                ("C04", "截图可读与完整"),
                ("D02", "截图文件有效"),
                ("D03", "证据关联完整"),
            ):
                add(code, title, "待补充", reason="尚无可精确关联到该商品的渠道与证据记录")
        add(
            "B04",
            "最低有效价",
            "待复核" if eligible else "待补充",
            " / ".join(t.price for t in eligible),
            "有效价格须先完成来源、规格及优惠口径核验",
        )
        add(
            "D04",
            "数据与证据版本",
            "待复核",
            f"{self.month.year}年{self.month.month}月；工作簿SHA256 {digest(self.quote_path)}",
            "取价时间和证据有效期限口径尚需确认",
        )
        k, purchase, m = (money(p.values.get(c)) for c in ("K", "L", "M"))

        def comparison(code, title, left, right, text):
            add(
                code,
                title,
                "待补充"
                if left is None or right is None
                else ("通过" if left <= right else "未通过"),
                text,
                "需有效正数输入" if left is None or right is None else "采用精确十进制比较",
            )

        comparison(
            "E01",
            "采购价与全国均价",
            purchase,
            m,
            f"L {p.values.get('L', '')} ≤ M {p.values.get('M', '')}",
        )
        if category == "手机":
            comparison(
                "E02",
                "手机加价率上限",
                k,
                _phone_limit(purchase) if purchase else None,
                f"K {p.values.get('K', '')} ≤ L×1.045 {_phone_limit(purchase) if purchase else '待补充'}",
            )
        else:
            add(
                "E02",
                "手机加价率上限",
                "待复核",
                category,
                "分类或多形态适用规则待明确，不能套用手机规则",
            )
        previous = money(source.cells.get("J"))
        history = supplied_history(p)
        bounds = [v for _, v in history] + ([previous] if previous is not None else [])
        history_min = min(bounds) if bounds else None
        history_detail = '；'.join(f'{period}：{value}' for period, value in history)
        history_fail = k is not None and history_min is not None and k > history_min
        add(
            "E03",
            "适用历史报价",
            "未通过" if history_fail else ("待补充" if k is None or history_min is None else "待复核"),
            f"当月 {k if k is not None else '待填写'}；已知历史上限 {history_min if history_min is not None else '待补充'}",
            ("当月高于已提供历史报价。" if history_fail else "已提供月份的数值比较未超上限；请确认这些月份覆盖本次适用历史范围。")
            + (p.context.get('history_source', '') + '；' + history_detail if history else "当前仅有报价表J列上期价格；需要确认是否还应提供其他历史月份。"),
            human=k is not None and history_min is not None and not history_fail,
        )
        warehouse_raw = p.context.get('warehouse_price', '')
        q, o = money(p.values.get("Q")), warehouse_limit(warehouse_raw)
        known_fail = k and any(v is not None and k > v for v in (q, o))
        add(
            "E04",
            "零售与一级库上限",
            "未通过" if known_fail else ("待补充" if not k or not q or not o else "待复核"),
            f"当月 {k if k is not None else '待填写'}；分销 {q if q is not None else '待填写'}；一级库 {o if o is not None else (warehouse_raw or '待补充')}",
            ("高于已知价格上限" if known_fail else "分销/一级库数值符合时，还须核验规定渠道有效官方零售价")
            + f"；营销建议零售价：{warehouse_raw or '缺失'}；采用区间上限：{o if o is not None else '无法识别，请核对源表'}；{p.context.get('warehouse_source', '报价表O列')}",
        )
        stock = p.context.get("stock")
        add(
            "E05",
            "一级库在库资格",
            "未通过" if stock == "不在库" else ("通过" if stock == "在库" and (note or p.context.get('stock_source')) else "待补充"),
            stock or "未确认",
            ("营销表已匹配：" + p.context['stock_source']) if p.context.get('stock_source') else
            ("人工补充依据：" + note if note else p.context.get('marketing_source_error', '请关联营销商品信息表，自动核验在库记录')),
        )
        entry = _date(p.context.get("entry_date"))
        today = date(self.month.year, self.month.month, 1)
        # Quote month has no precise reporting day. Boundary month stays uncertain.
        month_end = date(today.year, today.month, monthrange(today.year, today.month)[1])
        valid_entry = entry is not None and date(1900, 1, 1) <= entry <= month_end
        boundary = _anniversary(entry, 14) if valid_entry else None
        over = bool(boundary and today > boundary)
        ambiguous = bool(boundary and boundary.year == today.year and boundary.month == today.month)
        youfu = p.context.get("youfu", "未确认")
        if not valid_entry:
            add(
                "E06",
                "超14个月优福包确认",
                "待补充",
                p.context.get("entry_date", ""),
                "请补充有效入总部一级库日期",
            )
        elif ambiguous:
            add(
                "E06",
                "超14个月优福包确认",
                "待复核",
                entry.isoformat(),
                "本月达到14个月；具体报送日期未明确，需确认适用时间",
            )
        elif over and youfu not in ("是", "否"):
            add(
                "E06",
                "超14个月优福包确认",
                "待复核",
                entry.isoformat(),
                "入库超过14个月，请确认是否优福包；尚未自动列入不报价",
            )
        elif over and youfu == "是":
            add(
                "E06",
                "超14个月优福包确认",
                "不适用",
                entry.isoformat(),
                "已人工确认优福包，列入不报价；须核对最终输出表达",
            )
        else:
            add(
                "E06",
                "超14个月优福包确认",
                "通过",
                entry.isoformat(),
                "已确认非优福包，继续其他校验" if over else "尚未超过14个月",
            )
        first = _date(p.context.get("first_quote_date"))
        if not first or not date(1900, 1, 1) <= first <= month_end:
            add(
                "E07",
                "周期调价与例外依据",
                "待补充",
                reason="请补充首次报价日期；不能以入库日期替代",
            )
        elif (
            _anniversary(first, 6)
            > date(today.year, today.month, monthrange(today.year, today.month)[1])
            and note
        ):
            add(
                "E07",
                "周期调价与例外依据",
                "不适用",
                first.isoformat(),
                "人工依据记录首次报价未满6个月",
            )
        else:
            add(
                "E07",
                "周期调价与例外依据",
                "待复核",
                first.isoformat(),
                "调价基准、超过12个月算法及例外确认流程未明确；AO及附件不自动放行",
                tuple(p.attachments),
            )
        current_version = digest(self.quote_path)
        rows = {}
        error = self._inspection_error
        try:
            if current_version != self._inspection_version:
                self._rows = inspect(self.quote_path, self.month)
                self._inspection_version = current_version
                self._inspection_error = error = ""
            rows = self._rows
        except ValueError as exc:
            error = str(exc)
        row = rows.get(p.output_row, {})
        identity = tuple(str(row.get(c, "") or "") for c in ("C", "D", "E", "F", "G"))
        located = (
            bool(row)
            and identity == self._identities[p.id]
            and str(row.get("C", "")) == p.material_code
        )
        add(
            "F01",
            "工作簿商品定位",
            "未检查" if error else ("通过" if located else "未通过"),
            f"5G手机!C{p.output_row} {p.material_code}",
            error or ("稳定行身份与输出一致" if located else "行身份不符或输出不存在"),
        )
        same = located and all(
            (
                str(row.get(c, "") or "") == p.values.get(c, "")
                if c == "AO"
                else (
                    (not str(row.get(c, "") or "") and not p.values.get(c, ""))
                    or (
                        money(row.get(c)) is not None
                        and money(row.get(c)) == money(p.values.get(c))
                    )
                )
            )
            for c in COLUMNS
        )
        filled = all(money(p.values.get(c)) for c in COLUMNS[:-1])
        add(
            "F02",
            "手工值填写与写回一致",
            "通过" if same and filled else "待补充",
            "；".join(f"{c}={p.values.get(c, '')}" for c in COLUMNS),
            "当前五项金额与已保存值一致"
            if same and filled
            else "补齐五项有效金额并保存；尚未写回内容不视为已确认",
        )
        add(
            "F03",
            "公式结构与计算结果",
            "待复核",
            reason="局部XML写回保留原公式与结构；未运行Excel计算，不能宣称结果已核验",
        )
        excluded = over and youfu == "是"
        add(
            "F04",
            "不报价与最终输出范围",
            "待复核",
            "不报价" if excluded else "草稿",
            "优福包不报价的工作簿表达尚未明确"
            if excluded
            else "本地草稿；最终范围与状态表达待核验",
        )
        add(
            "F05",
            "工作簿与依据最新版本",
            "通过"
            if current_version == self._version and current_version != "missing"
            else "未检查",
            current_version,
            "版本与当前会话一致"
            if current_version == self._version
            else "输出版本已变更，请重新打开并核验",
        )
        return checks

    def all_checks(self):
        return [c for p in self.products for c in self.evaluate(p)]

    def save(self, p):
        if self.running:
            raise ValueError("任务执行中，暂不能写回报价工作簿")
        if not any(item is p for item in self.products):
            raise ValueError("商品不属于当前批次")
        if not self.quote_path:
            raise ValueError("尚无报价工作簿")
        for a in p.attachments:
            if not a.is_file() or a.stat().st_size == 0:
                raise ValueError(f"依据附件不存在或为空：{a.name}")
        before = self._version
        backup = write_cells(self.quote_path, before, self.month, p, self._identities[p.id])
        prior_confirmed = dict(self._confirmed)
        self._version = digest(self.quote_path, fresh=True)
        self._confirmed.clear()
        self._history.append(
            {
                "action": "save_draft",
                "product_id": p.id,
                "time": _now(),
                "before_version": before,
                "workbook_version": self._version,
                "values": dict(p.values),
                "context": dict(p.context),
                "attachments": [(str(a), digest(a)) for a in p.attachments],
            }
        )
        try:
            self._persist()
        except OSError:
            rollback(self.quote_path, backup, self._version)
            self._version = before
            self._confirmed = prior_confirmed
            self._history.pop()
            raise
        return self.quote_path

    def confirm_product(self, p):
        if self.running or not can_confirm(self.evaluate(p)):
            raise ValueError("仍有未通过、待处理或未检查项目，不能确认本品报价")
        path = self.save(p)
        if not can_confirm(self.evaluate(p)):
            raise ValueError("保存后依据版本需要重新核验，未标记确认")
        self._confirmed[p.id] = self._fingerprint(p)
        self._persist()
        return path

    def review(self, check_id, conclusion, reason, operator):
        if conclusion not in ("通过", "未通过") or not reason.strip() or not operator.strip():
            raise ValueError("请选择复核结论，并填写理由与操作人")
        c = next((c for c in self.all_checks() if c.id == check_id), None)
        if c is None or not c.human_reviewable or c.status not in ("待复核", "通过", "未通过"):
            raise ValueError("本项为确定性规则、资料缺失或未定义政策，不能人工绕过")
        p = next(p for p in self.products if p.id == c.product_id)
        record = {
            "action": "human_review",
            "check_id": c.id,
            "product_id": p.id,
            "before_status": c.status,
            "conclusion": conclusion,
            "reason": reason.strip(),
            "operator": operator.strip(),
            "time": _now(),
            "version": self._fingerprint(p),
        }
        previous = self._reviews.get(c.id)
        self._reviews[c.id] = record
        self._history.append(record)
        self._confirmed.clear()
        try:
            self._persist()
        except OSError:
            self._history.pop()
            if previous is None:
                self._reviews.pop(c.id, None)
            else:
                self._reviews[c.id] = previous
            raise

    def export_report(self, final=False):
        if final:
            digest(self.quote_path, fresh=True)
            for p in self.products:
                for path in p.attachments + [
                    t.evidence_path for t in p.channels if t.evidence_path
                ]:
                    digest(path, fresh=True)
        checks = self.all_checks()
        if final and (
            self.running
            or not can_confirm(checks)
            or any(self._confirmed.get(p.id) != self._fingerprint(p) for p in self.products)
        ):
            raise ValueError("全批次最新报价与证据尚未完成确认，只能导出草稿")
        if self.quote_path is None:
            raise ValueError("尚无报价输出目录")

        def esc(v):
            return html.escape(str(v), quote=True)

        mode = "最终确认版" if final else "草稿（含未解决事项）"
        pieces = [
            f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>报价稽核{mode}</title><style>body{{font:15px sans-serif;margin:32px;color:#182636}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:8px;text-align:left}}h2{{margin-top:32px}}.note{{color:#854800}}</style><h1>报价稽核报告 · {mode}</h1>',
            f"<p>批次 {esc(self.batch_id)} · {self.month.year}年{self.month.month}月 · {esc(_now())}</p>",
            f"<p>规则版本 {RULE_VERSION} · 工作簿版本 {digest(self.quote_path)}</p>",
            f"<p>商品 {len(self.products)} · 检查 {len(checks)} · 未解决 {sum(c.status not in ('通过', '不适用') for c in checks)}</p>",
        ]
        for category in CATEGORIES:
            pieces.append(
                f"<p>{category}：{esc(dict(Counter(c.status for c in checks if c.category == category)))}</p>"
            )
        for p in self.products:
            pieces.append(
                f"<h2>{esc(p.title)} / {esc(p.specification)} / {esc(p.material_code)}</h2><p>稳定商品标识 {p.id} · 5G手机第{p.output_row}行</p><table><tr><th>检查</th><th>状态</th><th>比较</th><th>依据</th></tr>"
            )
            for c in checks:
                if c.product_id == p.id:
                    links = " ".join(
                        f'<a href="{esc(a.resolve().as_uri())}">{esc(a.name)}</a>'
                        for a in c.evidence_paths
                    )
                    pieces.append(
                        f"<tr><td>{esc(c.code + ' ' + c.title)}</td><td>{esc(c.status)}</td><td>{esc(c.comparison)}</td><td>{esc(c.reason)} {links}</td></tr>"
                    )
            pieces.append("</table>")
        pieces.append(
            "<h2>人工处理与保存记录</h2><pre>"
            + esc(json.dumps(self._history, ensure_ascii=False, indent=2))
            + "</pre></html>"
        )
        path = self.quote_path.with_name(
            self.quote_path.stem
            + ".稽核"
            + ("最终版" if final else "草稿")
            + "."
            + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            + ".html"
        )
        atomic_json(path, "".join(pieces))
        return path

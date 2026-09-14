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
from quote_app.core.formulas import formula_cells
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
from quote_app.services.screenshot_review import request_scan
from quote_app.services.channel_audit import inspect_channel

CATEGORIES = (
    "商品分类与关联",
    "渠道与价格口径",
    "截图内容核验",
    "证据与数据完整性",
    "报价规则与资格",
    "报表与报送一致性",
)
RULE_VERSION = "decision-audit-2026-09-14-v2"


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
    channel: str = ''


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


def can_confirm_decision(checks):
    rules = [c for c in checks if c.code.startswith('E')]
    return ({c.code for c in rules} == {f'E{i:02}' for i in range(1, 10)}
            and can_confirm(rules)
            and not any(c.code == 'E06' and c.status == '不适用' for c in rules))


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


def profit_trial(purchase, settlement):
    purchase, settlement = money(purchase), money(settlement)
    if purchase is None or settlement is None:
        return None
    with localcontext() as context:
        context.prec = max(40, len(purchase.as_tuple().digits) + len(settlement.as_tuple().digits) + 10)
        difference = settlement - purchase
        return {'difference': difference, 'margin': difference / settlement * 100,
                'markup': difference / purchase * 100}


def collected_offers(product):
    """Collection observations are decision inputs; image audit remains independent."""
    return [t for t in product.channels if t.channel in ('jd', 'tmall', 'official')
            and t.outcome == 'price_found' and money(t.price) is not None
            and (not t.material_code or t.material_code == product.material_code)]


def periodic_adjustment(product):
    first = _date(product.context.get('first_quote_date'))
    month = _date(product.context.get('quote_month'))
    if not first or not month or first < date(1900, 1, 1) or first > date(month.year, month.month, monthrange(month.year, month.month)[1]):
        return None, None, None
    # Monthly quotations: first month is month 1, month 7 starts the 5% step.
    elapsed = (month.year - first.year) * 12 + month.month - first.month
    reduction = Decimal('0.10') if elapsed >= 12 else Decimal('0.05') if elapsed >= 6 else Decimal('0')
    base = money(product.context.get('first_quote_price'))
    with localcontext() as ctx:
        ctx.prec = max(40, len(base.as_tuple().digits) + 5) if base else 40
        limit = base * (1 - reduction) if base and reduction else None
    return elapsed, reduction, limit


def price_ceiling(product, checks=()):
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
    for task in collected_offers(product):
        candidates.append((money(task.price), '本批次已取渠道最低价'))
    _, reduction, periodic_limit = periodic_adjustment(product)
    if periodic_limit is not None:
        candidates.append((periodic_limit, f'首次报价下调{reduction * 100:.0f}%'))
    if not candidates:
        return None, "尚无可计算约束；渠道有效性及其他资格仍须核验"
    low = min(v for v, _ in candidates)
    return low, "、".join(
        dict.fromkeys(label for v, label in candidates if v == low)
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
        self.batch_reviewer = ""
        self.product_reviewers = {}
        self._replacements = {}
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
        if self.quote_path and not self.load_error:
            from .review_delivery import recover_basis_images
            for product in self.products:
                if not product.attachments or any(not a.is_file() for a in product.attachments):
                    try:
                        recovered = recover_basis_images(self.quote_path, product.output_row)
                    except (OSError, ValueError, KeyError) as error:
                        self.load_error = f"依据图片读取失败：{error}"
                        break
                    if recovered:
                        product.attachments = recovered
        for p in self.products:
            p.context["quote_month"] = f"{month.year:04d}-{month.month:02d}-01"
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
        run_ids, source_rows = [], []
        paths = matching_run_paths(path, month, rows, task_database, run_ids=run_ids, source_rows=source_rows)
        paths.update({k: Path(v) for k, v in (source_paths or {}).items() if v and Path(v).is_file() and k not in paths})
        result = cls(
            DesktopState(quote_rows=tuple(source), quote_path=Path(path)),
            month,
            _output_rows=[row.source_row_number for row in source],
            _source_metadata=False,
            source_paths=paths,
        )
        if source_rows:
            from quote_app.domain.models import WebQuery
            for product, original in zip(result.products, source_rows):
                result._sources[product.id] = QuoteRow(
                    original['source_row_number'], original['material_code'], original['cells'],
                    [Issue(**issue) for issue in original.get('issues', [])],
                    WebQuery(**original.get('web_query', {})))
                result._source_known[product.id] = True
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
        for product in result.products:
            result._apply_replacements(product)
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
            self.batch_reviewer = data.get("batch_reviewer", "")
            self.product_reviewers = data.get("product_reviewers", {})
            self._confirmed = data.get("confirmed", {})
            if data.get("workbook_version") == self._version:
                self._replacements = data.get("replacements", {})
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
                    # Migrate only text known to have been entered through the old UI.
                    if data.get('notes_column') != 'AP' and not p.values.get('AP') and saved.get('values', {}).get('AO'):
                        p.values['AP'] = saved['values']['AO']
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
            "batch_reviewer": self.batch_reviewer,
            "product_reviewers": self.product_reviewers,
            "replacements": self._replacements,
            "reviews": self._reviews,
            "history": self._history,
            "confirmed": self._confirmed,
            "notes_column": "AP",
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
        self._apply_replacements(p)
        checks = []
        fingerprint = self._fingerprint(p)

        def add(code, title, status, comparison="", reason="", paths=(), human=False, suffix="", channel=""):
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
                channel,
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
        association_issues = [i for i in source.issues if "MARKETING" in i.code or "BOP" in i.code]
        output_identity = tuple(str(self._rows.get(p.output_row, {}).get(c, '') or '') for c in ('C', 'D', 'E', 'F', 'G'))
        identity_ok = output_identity == self._identities[p.id]
        source_ok = self._source_known[p.id] and bool(p.material_code) and identity_ok and not association_issues
        add('A02', '商品与源表一致性',
            '未通过' if not identity_ok or association_issues else '通过' if source_ok else '待补充',
            p.title + ' · ' + p.specification,
            '物料编码及商品规格与本批次源表关联记录一致，未发现营销或资源关联冲突' if source_ok else
            '；'.join(i.message for i in association_issues) or ('输出商品与源表身份不一致' if not identity_ok else '未找到本批次原始关联记录，请关联本批次源表'))
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
            "未通过" if duplicate else "通过" if all(self._source_known.values()) and len(self._rows) == len(self.products) else "待复核",
            f"本批次{len(self.products)}条商品",
            "存在重复编码与规格" if duplicate else "输出商品数量与已关联源表范围一致，未发现重复" if all(self._source_known.values()) and len(self._rows) == len(self.products) else "未见同编码同规格重复；源表完整范围仍需核对",
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
        for t in p.channels:
            paths = (t.evidence_path,) if t.evidence_path else ()
            scan = None
            if t.evidence_path and t.evidence_path.is_file():
                try:
                    scan = request_scan(t.evidence_path)
                except (OSError, ValueError):
                    pass
            column = {'jd': 'AI', 'tmall': 'AJ', 'official': 'AK'}.get(t.channel)
            written = self._rows.get(p.output_row, {}).get(column)
            findings = inspect_channel(t, p.title, t.specification or p.specification, scan, written)
            reused = t.evidence_path and sum(x.evidence_path == t.evidence_path for item in self.products for x in item.channels) > 1
            if reused or (t.material_code and t.material_code != p.material_code):
                findings[-1].status = '未通过'
                findings[-1].reason = '同一截图被多条记录引用或商品关联不一致，请修正证据关联'
                for item in findings[:3]:
                    item.status, item.reason = '未检查', '等待修正截图关联'
            [add(f.code, f.title, f.status, f.comparison, f.reason, paths,
                           human=f.status == '待复核', suffix=t.task_id, channel=t.channel) for f in findings]
        for missing_channel in sorted({'jd', 'tmall', 'official'} - {t.channel for t in p.channels}):
            for finding in inspect_channel(TaskRow('missing', channel=missing_channel), p.title, p.specification, None, None):
                add(finding.code, finding.title, '未检查', '等待渠道记录', '请先关联本批次渠道采集记录',
                    suffix='missing-' + missing_channel, channel=missing_channel)
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
        first = _date(p.context.get('first_quote_date'))
        first_month = bool(first and (first.year, first.month) == (self.month.year, self.month.month))
        add(
            "E03",
            "历史报价上限",
            "未通过" if history_fail else ("不适用" if history_min is None and first_month else "待补充" if k is None or history_min is None else "通过"),
            f"当月 {k if k is not None else '待填写'}；已知历史上限 {history_min if history_min is not None else '待补充'}",
            ("当月高于已提供历史报价。" if history_fail else "已按本次提供的有效往期报价自动比较。" if history_min is not None else
             "用户确认本月首次报价，且本批次未发现有效往期报价。" if first_month else "缺少有效历史报价，请关联基础表；首次报价请填写实际首次报价日期。")
            + (p.context.get('history_source', '') + '；' + history_detail if history else "；采用报价表J列已知上期价格" if previous is not None else ''),
        )
        warehouse_raw = p.context.get('warehouse_price', '')
        q, o = money(p.values.get("Q")), warehouse_limit(warehouse_raw)
        comparison('E04', '分销零售价上限', k, q,
                   f"当月 {k if k is not None else '待填写'}；分销零售价 {q if q is not None else '待填写'}")
        comparison('E08', '一级库价格上限', k, o,
                   f"当月 {k if k is not None else '待填写'}；一级库上限 {o if o is not None else '待补充'}")
        checks[-1].reason += f"；建议零售价：{warehouse_raw or '缺失'}；采用区间上端；{p.context.get('warehouse_source', '报价表O列')}"
        offers = collected_offers(p)
        external_limit = min((money(t.price) for t in offers), default=None)
        if external_limit is not None:
            cheapest = '、'.join({'jd': '京东', 'tmall': '天猫', 'official': '官网'}[t.channel]
                                for t in offers if money(t.price) == external_limit)
            comparison('E09', '三网已取最低价上限', k, external_limit,
                       f'当月 {k if k is not None else "待填写"} ≤ {cheapest} {external_limit}')
            checks[-1].reason += (f'；依据本批次{len(offers)}条已取价格自动试算，不等待最终截图稽核。'
                                 '截图、店铺及优惠口径由稽核工作台独立复核；采集或稽核纠正价格后须重新试算。')
        elif {t.channel for t in p.channels if t.state == 'succeeded' and t.outcome == 'no_model'} == {'jd', 'tmall', 'official'}:
            add('E09', '三网已取最低价上限', '不适用', '三个渠道均无匹配机型', '本批次三个渠道均返回无机型；最终仍需核验截图依据')
        else:
            add('E09', '三网已取最低价上限', '待补充', '暂无已取价格', '请完成取价或关联本批次取价记录；没有有效金额不能自动通过')
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
        elapsed, reduction, periodic_limit = periodic_adjustment(p)
        if elapsed is None:
            add('E07', '周期调价与例外依据', '待补充', reason='请选择有效首次报价日期；以本次报价月份判断，不能用入库日期替代')
        elif not reduction:
            add('E07', '周期调价与例外依据', '不适用', f'首次报价 {p.context["first_quote_date"]}；第{elapsed + 1}个月',
                '尚未进入第7个月，暂不触发周期降价；仍需满足其他价格上限')
        elif periodic_limit is None:
            add('E07', '周期调价与例外依据', '待补充', f'应较首次报价下调{reduction * 100:.0f}%',
                '已到调价周期，请在“补充资格与日期依据”填写首次报价金额，程序将自动比较；不能用最近一期报价代替首次报价')
        else:
            comparison('E07', '周期调价与例外依据', k, periodic_limit,
                       f'当月 {k if k is not None else "待填写"} ≤ 首报{p.context["first_quote_price"]}×{(1-reduction)*100:.0f}% = {periodic_limit}')
            checks[-1].reason += (f'；首次报价 {p.context["first_quote_date"]}；按报价月份已跨{elapsed}个月，'
                                 f'应较首次报价下调{reduction*100:.0f}%；不重复累计下调。')
            if k is not None and k > periodic_limit:
                checks[-1].reason += '如确实无法调价，请在AP说明填写理由并关联厂家依据；附件存在不代表例外已获确认。'
                if p.values.get('AP', '').strip() and any(a.is_file() and a.stat().st_size for a in p.attachments):
                    checks.pop()
                    add('E07', '周期调价与例外依据', '待复核',
                        f'当月 {k} > 调价上限 {periodic_limit}；已提交例外依据',
                        '自动比较未满足降价要求，仅例外理由的合理性需要人工复核；可在稽核工作台确认例外，或调整报价后自动通过',
                        tuple(p.attachments), human=True)
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
                if c == "AP"
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
        mismatches = [f'{col}{p.output_row}' for col, formula in formula_cells(p.output_row).items()
                      if row.get(col) != formula]
        add('F03', '报价表计算公式是否完整', '未检查' if error else '未通过' if mismatches else '通过',
            '公式缺失或被修改：' + '、'.join(mismatches) if mismatches else 'X、Y、Z、AA四列公式与程序模板一致',
            error or '自动核对上期/历史降幅、采购加价率及结算金额四列公式和引用行。此项检查公式完整性；Excel缓存计算结果未在此宣称已重算。公式异常请重新生成报价表。',
            (self.quote_path,) if self.quote_path else ())
        excluded = over and youfu == '是'
        still_quoted = str(row.get('K') or '').strip() != ''
        exclusion_reason = ('已列入不报价，须清空当月报价并核对最终报送范围；不能保留金额直接报送' if still_quoted else '不报价商品当月报价为空，未混入有价格的报价范围') if excluded else '仅检查已确认超14个月优福包的不报价商品；草稿状态本身不需要人工复核'
        add('F04', '不报价商品是否仍填写报价',
            '未检查' if error else '未通过' if excluded and still_quoted else '通过' if excluded and located else '不适用' if not excluded else '未通过',
            f'已确认优福包；表内当月报价：{row.get("K", "空白")}' if excluded else '本商品未被列入不报价',
            error or exclusion_reason,
            (self.quote_path,) if self.quote_path else ())
        version_ok = current_version == self._version and current_version != 'missing'
        add('F05', '当前报价文件是否被外部修改', '通过' if version_ok else '未检查',
            self.quote_path.name if self.quote_path else '暂无报价文件',
            '文件与当前会话一致，程序已自动校验；内部文件指纹保留在稽核报告中，无需人工核对' if version_ok else '文件已变化或缺失，请重新打开本批次后自动核验',
            (self.quote_path,) if self.quote_path else ())
        return checks

    def all_checks(self):
        return [c for p in self.products for c in self.evaluate(p)]

    def is_written(self, p):
        """Current five amounts and remarks match the actual workbook, not a click flag."""
        current = digest(self.quote_path)
        if current != self._version:
            return False
        if current != self._inspection_version:
            try:
                self._rows = inspect(self.quote_path, self.month)
                self._inspection_version = current
            except ValueError:
                return False
        row = self._rows.get(p.output_row, {})
        from .review_delivery import basis_images
        image_ok = not p.attachments or [hashlib.sha256(b).hexdigest() for b in basis_images(self.quote_path, p.output_row)] == [digest(a) for a in p.attachments]
        return image_ok and all(money(p.values.get(c)) is not None and money(p.values.get(c)) == money(row.get(c))
                   for c in COLUMNS[:-1]) and str(row.get('AP') or '') == p.values.get('AP', '')

    def _rebase_owned_write(self, fingerprints, *, exclude=()):
        """Only an app-owned, verified mutation may carry existing approvals forward.

        An external edit never comes through here and still invalidates every old fingerprint.
        Already stale approvals (changed values/context/evidence) are never revived.
        """
        for p in self.products:
            if p.id in exclude:
                # Retain old fingerprint to display "修改后待重新确认".
                continue
            old, new = fingerprints[p.id], self._fingerprint(p)
            if self._confirmed.get(p.id) == old:
                self._confirmed[p.id] = new
            for key, record in list(self._reviews.items()):
                if record.get('product_id') == p.id and record.get('version') == old:
                    self._reviews[key] = {**record, 'version': new}

    def reviewer_for(self, p):
        return self.product_reviewers.get(p.id, self.batch_reviewer)

    def set_reviewer(self, name, p=None):
        name = name.strip()
        if not name:
            raise ValueError('请填写稽核人姓名')
        if p is not None and not any(item is p for item in self.products):
            raise ValueError('商品不属于当前批次')
        previous = self.batch_reviewer, dict(self.product_reviewers)
        if p is None:
            self.batch_reviewer = name
        else:
            self.product_reviewers[p.id] = name
        try:
            self._persist()
        except OSError:
            self.batch_reviewer, self.product_reviewers = previous
            raise

    def _apply_replacements(self, p):
        for record in self._replacements.get(p.id, {}).values():
            data = dict(record)
            data['evidence_path'] = Path(data['evidence_path'])
            replacement = TaskRow(**data)
            p.channels = [t for t in p.channels if t.channel != replacement.channel] + [replacement]

    def repair_channel(self, p, channel, source_path, price, url, reason):
        from urllib.parse import urlsplit
        price_value = money(price)
        url = url.strip()
        parsed = urlsplit(url)
        if price_value is None:
            raise ValueError('请填写大于0的有效渠道价格')
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in url):
            raise ValueError('请粘贴完整的HTTP/HTTPS商品来源链接')
        return self.replace_evidence(p, channel, source_path, reason, price=str(price_value), url=url)

    def replace_evidence(self, p, channel, source_path, reason, *, price=None, url=None):
        from dataclasses import replace
        from uuid import uuid4
        from .review_evidence import image_bytes, write_evidence
        if self.running or self.quote_path is None:
            raise ValueError('请等待任务结束后补充截图')
        if not any(item is p for item in self.products) or channel not in ('official', 'tmall', 'jd'):
            raise ValueError('请选择本批次商品与渠道')
        if not reason.strip():
            raise ValueError('请填写补充或替换截图的原因')
        if digest(self.quote_path, fresh=True) != self._version:
            raise ValueError('报价文件已被外部修改，请重新打开本批次')
        payload, extension = image_bytes(source_path)
        self.evaluate(p)
        old = next((t for t in p.channels if t.channel == channel), None)
        from .review_sources import CHANNEL_COLUMNS
        if old is None:
            old_price = self._rows.get(p.output_row, {}).get(CHANNEL_COLUMNS[channel][0])
            old = TaskRow(f'workbook:{p.output_row}:{channel}', p.title, channel,
                          price=str(old_price) if money(old_price) else '', outcome='price_found' if money(old_price) else '',
                          state='workbook', source_row_number=self._sources[p.id].source_row_number,
                          material_code=p.material_code, specification=p.specification)
        fingerprints = {item.id: self._fingerprint(item) for item in self.products}
        previous_version = self._version
        previous_confirmed = dict(self._confirmed)
        previous_reviews = {key: dict(value) for key, value in self._reviews.items()}
        previous_replacements = {key: dict(value) for key, value in self._replacements.items()}
        previous_channels = list(p.channels)
        previous_model_rows = list(self.model.rows)
        folder = self.quote_path.parent / '截图证据' / '人工补充'
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (uuid4().hex + '.' + extension)
        destination.write_bytes(payload)
        backup = None
        try:
            from .review_delivery import staged_workbook, commit_staged, write_channel_values
            with staged_workbook(self.quote_path, previous_version) as staged:
                write_evidence(staged, previous_version, self.month, p,
                               self._identities[p.id], channel, payload, extension, backup=False)
                if price is not None:
                    write_channel_values(staged, self.month, p, self._identities[p.id], channel, price, url,
                                         {CHANNEL_COLUMNS[t.channel][0]: t.url for t in p.channels if t.channel in CHANNEL_COLUMNS})
                backup = commit_staged(self.quote_path, previous_version, staged)
            self._version = digest(self.quote_path, fresh=True)
            updated = replace(old, evidence_path=destination, evidence_state='complete')
            if price is not None:
                updated = replace(updated, price=price, url=url, state='manual_corrected', outcome='price_found', error='')
            self._replacements.setdefault(p.id, {})[channel] = {
                **asdict(updated), 'evidence_path': str(destination)}
            self._apply_replacements(p)
            if price is not None:
                self.model.rows = [updated if t.task_id == old.task_id else t for t in self.model.rows]
            self._rebase_owned_write(fingerprints, exclude=(p.id,))
            self._history.append({'action': 'repair_channel' if price is not None else 'replace_evidence', 'product_id': p.id, 'channel': channel,
                'reason': reason.strip(), 'operator': self.reviewer_for(p) or '产品经理补充', 'time': _now(),
                'old_evidence': str(old.evidence_path or ''), 'old_digest': digest(old.evidence_path),
                'new_evidence': str(destination), 'new_digest': digest(destination),
                'before_version': previous_version, 'workbook_version': self._version,
                'old_price': old.price, 'new_price': updated.price, 'old_url': old.url, 'new_url': updated.url, 'price_unchanged': old.price if price is None else None})
            try:
                self._persist()
            except OSError:
                self._history.pop()
                raise
        except Exception:
            if backup is not None:
                rollback(self.quote_path, backup, self._version)
            self._version = previous_version
            self._confirmed, self._reviews = previous_confirmed, previous_reviews
            self._replacements, p.channels = previous_replacements, previous_channels
            self.model.rows = previous_model_rows
            destination.unlink(missing_ok=True)
            raise
        return destination

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
        fingerprints = {item.id: self._fingerprint(item) for item in self.products}
        prior_reviews = {key: dict(value) for key, value in self._reviews.items()}
        from .review_delivery import staged_workbook, commit_staged, save_basis_images
        with staged_workbook(self.quote_path, before) as staged:
            write_cells(staged, before, self.month, p, self._identities[p.id], backup=False)
            save_basis_images(staged, digest(staged, fresh=True), self.month, p, self._identities[p.id])
            backup = commit_staged(self.quote_path, before, staged)
        prior_confirmed = dict(self._confirmed)
        self._version = digest(self.quote_path, fresh=True)
        self._rebase_owned_write(fingerprints)
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
            self._reviews = prior_reviews
            self._history.pop()
            raise
        return self.quote_path

    def confirm_product(self, p):
        if self.running or not can_confirm_decision(self.evaluate(p)):
            raise ValueError("报价规则仍有未解决事项，或本品已列入不报价，不能确认本品报价")
        path = self.save(p)
        saved_checks = self.evaluate(p)
        if not can_confirm_decision(saved_checks) or not can_confirm(
            [c for c in saved_checks if c.code in ('F01', 'F02', 'F05')]
        ):
            raise ValueError("保存后报价规则或写回数据未通过核验，未标记确认")
        previous = dict(self._confirmed)
        self._confirmed[p.id] = self._fingerprint(p)
        try:
            self._persist()
        except OSError:
            self._confirmed = previous
            raise
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
        prior_confirmed = dict(self._confirmed)
        # Reviewing evidence does not change the separately confirmed quotation.
        # Final export always evaluates the latest review conclusions.
        try:
            self._persist()
        except OSError:
            self._confirmed = prior_confirmed
            self._history.pop()
            if previous is None:
                self._reviews.pop(c.id, None)
            else:
                self._reviews[c.id] = previous
            raise

    def confirmation_status(self, p):
        prior = self._confirmed.get(p.id)
        if prior is None:
            return '未确认'
        return '已确认' if prior == self._fingerprint(p) else '修改后待重新确认'

    def unconfirmed_products(self):
        return [p for p in self.products if self._confirmed.get(p.id) != self._fingerprint(p)]

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
            or self.unconfirmed_products()
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
                f"<h2>{esc(p.title)} / {esc(p.specification)} / {esc(p.material_code)}</h2><p>产品经理确认：{esc(self.confirmation_status(p))}</p><p>稳定商品标识 {p.id} · 5G手机第{p.output_row}行</p><table><tr><th>检查</th><th>状态</th><th>比较</th><th>依据</th></tr>"
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

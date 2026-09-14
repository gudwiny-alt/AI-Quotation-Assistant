"""Atomic post-generation edits; original drawings/formula parts stay intact."""

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from tempfile import mkstemp
import hashlib
import os
import re
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from openpyxl import load_workbook
from .review_workbook import digest, inspect, make_backup, NS, money
from .review_evidence import R, REL, _target


@contextmanager
def staged_workbook(path, expected):
    if digest(path, fresh=True) != expected:
        raise ValueError("报价文件版本已被外部修改，请重新打开本批次")
    fd, name = mkstemp(prefix=".review-stage-", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    staged = Path(name)
    staged.write_bytes(path.read_bytes())
    try:
        yield staged
    finally:
        staged.unlink(missing_ok=True)


def commit_staged(path, expected, staged):
    if digest(path, fresh=True) != expected:
        raise ValueError("报价文件版本已被外部修改，请重新打开本批次")
    if staged.read_bytes() == path.read_bytes():
        return None
    backup = make_backup(path, expected)
    os.replace(staged, path)
    return backup


@lru_cache(maxsize=4)
def _basis_contents(path, version):
    book = load_workbook(path)
    images = {}
    try:
        for im in book["5G手机"]._images:
            marker = getattr(im.anchor, "_from", None)
            if marker is not None and marker.col == 40:
                images.setdefault(marker.row + 1, []).append(im._data())
        return images
    finally:
        book.close()


def basis_images(path, row):
    """Cache by verified workbook version; repeated UI refreshes avoid decoding images."""
    return _basis_contents(str(path), digest(path)).get(row, [])


def _remove_basis_anchors(path, row):
    # Replacement is restricted to AO on this exact product row.
    from .review_evidence import D

    with ZipFile(path) as z:
        info = z.infolist()
        parts = {n: z.read(n) for n in z.namelist()}
    workbook = ET.fromstring(parts["xl/workbook.xml"])
    sheet = next(e for e in workbook.find(f"{{{NS}}}sheets") if e.get("name") == "5G手机")
    relations = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    part = _target(
        "xl/workbook.xml",
        next(e.get("Target") for e in relations if e.get("Id") == sheet.get(f"{{{R}}}id")),
    )
    root = ET.fromstring(parts[part])
    drawing = root.find(f"{{{NS}}}drawing")
    if drawing is None:
        return
    from .review_evidence import _rels

    rels = ET.fromstring(parts[_rels(part)])
    drawing_part = _target(
        part, next(e.get("Target") for e in rels if e.get("Id") == drawing.get(f"{{{R}}}id"))
    )
    tree = ET.fromstring(parts[drawing_part])
    for anchor in list(tree):
        start = anchor.find(f"{{{D}}}from")
        if (
            start is not None
            and start.findtext(f"{{{D}}}col") == "40"
            and start.findtext(f"{{{D}}}row") == str(row - 1)
        ):
            if anchor.find(f"{{{D}}}pic") is None:
                raise ValueError("AO 依据位置包含非图片对象，请先调整该对象的位置")
            tree.remove(anchor)
    parts[drawing_part] = ET.tostring(tree)
    with ZipFile(path, "w") as z:
        for item in info:
            z.writestr(item, parts[item.filename])


def save_basis_images(path, expected, month, product, identity):
    from .review_evidence import image_bytes, write_evidence

    desired = [image_bytes(p) for p in product.attachments]
    if not desired:
        return
    old = basis_images(path, product.output_row)
    if [hashlib.sha256(p).digest() for p in old] == [
        hashlib.sha256(p[0]).digest() for p in desired
    ]:
        return
    _remove_basis_anchors(path, product.output_row)
    for i, (payload, extension) in enumerate(desired):
        write_evidence(
            path,
            digest(path, fresh=True),
            month,
            product,
            identity,
            "official",
            payload,
            extension,
            basis_slot=i,
            basis_count=len(desired),
            backup=False,
        )


def recover_basis_images(path, row):
    from tempfile import gettempdir

    folder = Path(gettempdir()) / "quotation-review-basis"
    folder.mkdir(exist_ok=True)
    result = []
    for payload in basis_images(path, row):
        destination = folder / (hashlib.sha256(payload).hexdigest() + ".png")
        if not destination.exists() or destination.read_bytes() != payload:
            destination.write_bytes(payload)
        result.append(destination)
    return result


def write_channel_values(path, month, product, identity, channel, price, url, channel_urls):
    """Write AI/AJ/AK + its hyperlink, and the selected minimum in AH."""
    from .review_sources import CHANNEL_COLUMNS

    rows = inspect(path, month)
    row = rows[product.output_row]
    if tuple(str(row.get(c, "") or "") for c in ("C", "D", "E", "F", "G")) != identity:
        raise ValueError("商品行定位不一致，已取消渠道修正")
    column = CHANNEL_COLUMNS[channel][0]
    amounts = {c: money(row.get(c)) for c in ("AI", "AJ", "AK")}
    amounts[column] = money(price)
    best = min((v, i, c) for i, (c, v) in enumerate(amounts.items()) if v is not None)
    minimum, _, best_column = best
    with ZipFile(path) as z:
        info = z.infolist()
        parts = {n: z.read(n) for n in z.namelist()}
    book = ET.fromstring(parts["xl/workbook.xml"])
    sheet = next(s for s in book.find(f"{{{NS}}}sheets") if s.get("name") == "5G手机")
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    sheet_part = _target(
        "xl/workbook.xml",
        next(r.get("Target") for r in rels if r.get("Id") == sheet.get(f"{{{R}}}id")),
    )
    from .review_evidence import _rels

    rel_part = _rels(sheet_part)
    relations = (
        ET.fromstring(parts[rel_part])
        if rel_part in parts
        else ET.Element(f"{{{REL}}}Relationships")
    )
    root = ET.fromstring(parts[sheet_part])
    target_row = next(
        r for r in root.find(f"{{{NS}}}sheetData") if r.get("r") == str(product.output_row)
    )
    links = root.find(f"{{{NS}}}hyperlinks")
    existing_urls = {}
    for link in links if links is not None else ():
        rid = link.get(f"{{{R}}}id")
        existing_urls[link.get("ref")] = next(
            (r.get("Target") for r in relations if r.get("Id") == rid), ""
        )
    if links is None:
        links = ET.Element(f"{{{NS}}}hyperlinks")
        after = {
            "sheetData",
            "sheetCalcPr",
            "sheetProtection",
            "protectedRanges",
            "scenarios",
            "autoFilter",
            "sortState",
            "dataConsolidate",
            "customSheetViews",
            "mergeCells",
            "phoneticPr",
            "conditionalFormatting",
            "dataValidations",
        }
        index = max((i for i, e in enumerate(root) if e.tag.split("}")[-1] in after), default=0)
        root.insert(index + 1, links)
    from uuid import uuid4

    updates = {
        column: (money(price), url),
        "AH": (
            minimum,
            url
            if column == best_column
            else channel_urls.get(best_column)
            or existing_urls.get(f"{best_column}{product.output_row}", ""),
        ),
    }
    updates["R"] = updates["AH"]
    updates["S"] = (updates["AH"][1] or "无", "")
    for col, (amount, link_url) in updates.items():
        address = f"{col}{product.output_row}"
        cell = next((c for c in target_row if c.get("r") == address), None)
        if cell is None:
            cell = ET.Element(f"{{{NS}}}c", r=address)
            from openpyxl.utils.cell import column_index_from_string

            offset = next(
                (
                    i
                    for i, c in enumerate(target_row)
                    if column_index_from_string("".join(x for x in c.get("r") if x.isalpha()))
                    > column_index_from_string(col)
                ),
                len(target_row),
            )
            target_row.insert(offset, cell)
        for child in list(cell):
            cell.remove(child)
        cell.attrib.pop("t", None)
        if col == "S":
            cell.set("t", "inlineStr")
            ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = amount
        else:
            ET.SubElement(cell, f"{{{NS}}}v").text = str(amount)
        for old in list(links):
            if old.get("ref") == address:
                links.remove(old)
        if link_url:
            rid = "rIdChannel" + uuid4().hex
            ET.SubElement(links, f"{{{NS}}}hyperlink", ref=address, attrib={f"{{{R}}}id": rid})
            ET.SubElement(
                relations,
                f"{{{REL}}}Relationship",
                Id=rid,
                Type=R + "/hyperlink",
                Target=link_url,
                TargetMode="External",
            )
    ET.register_namespace("", NS)
    # Preserve the worksheet root verbatim: compatibility attributes can refer
    # to namespace prefixes that ElementTree otherwise silently discards.
    xml = parts[sheet_part].decode("utf-8")
    row_pattern = rf'<row\b[^>]*\br="{product.output_row}"[^>]*>.*?</row>'
    match = re.search(row_pattern, xml, flags=re.S)
    if match is None:
        raise ValueError("无法定位报价表原始商品行，已取消渠道修正")
    original_row = match.group(0)
    for col in updates:
        address = f"{col}{product.output_row}"
        cell = next(c for c in target_row if c.get("r") == address)
        replacement = ET.tostring(cell, encoding="unicode")
        pattern = rf'<c\b[^>]*\br="{address}"(?=[\s/>])[^>]*?(?:/>|>.*?</c>)'
        if re.search(pattern, original_row, flags=re.S):
            original_row = re.sub(pattern, lambda _: replacement, original_row, count=1, flags=re.S)
        else:
            from openpyxl.utils.cell import column_index_from_string

            following = next(
                (
                    m
                    for m in re.finditer(r'<c\b[^>]*\br="([A-Z]+)\d+"', original_row)
                    if column_index_from_string(m.group(1)) > column_index_from_string(col)
                ),
                None,
            )
            pos = following.start() if following else original_row.rfind("</row>")
            original_row = original_row[:pos] + replacement + original_row[pos:]
    xml = xml[: match.start()] + original_row + xml[match.end() :]
    replacement = ET.tostring(links, encoding="unicode")
    pattern = r"<hyperlinks\b[^>]*?(?:/>|>.*?</hyperlinks>)"
    if re.search(pattern, xml, flags=re.S):
        xml = re.sub(pattern, lambda _: replacement, xml, count=1, flags=re.S)
    else:
        after_links = r"<(?:printOptions|pageMargins|pageSetup|headerFooter|rowBreaks|colBreaks|customProperties|cellWatches|ignoredErrors|smartTags|drawing|legacyDrawing|legacyDrawingHF|picture|oleObjects|controls|webPublishItems|tableParts|extLst)\b|</worksheet>"
        position = re.search(after_links, xml)
        if position is None:
            raise ValueError("无法定位报价表链接位置，已取消渠道修正")
        xml = xml[: position.start()] + replacement + xml[position.start() :]
    parts[sheet_part] = xml.encode("utf-8")
    parts[rel_part] = ET.tostring(relations)
    with ZipFile(path, "w") as z:
        for item in info:
            z.writestr(item, parts.pop(item.filename))
        for n, payload in parts.items():
            z.writestr(n, payload)
    actual = inspect(path, month)
    expected_rows = {n: dict(v) for n, v in rows.items()}
    for col, (amount, _) in updates.items():
        expected_rows[product.output_row][col] = amount if col == "S" else float(amount)
    if actual != expected_rows:
        raise ValueError("渠道价格写回校验未通过")
    verified = load_workbook(path)
    try:
        cell = verified["5G手机"][f"{column}{product.output_row}"]
        if (
            money(cell.value) != money(price)
            or cell.hyperlink is None
            or cell.hyperlink.target != url
        ):
            raise ValueError("渠道价格或链接写回校验未通过")
    finally:
        verified.close()

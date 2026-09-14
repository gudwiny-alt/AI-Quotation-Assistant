"""Replace one row/channel drawing without round-tripping the quotation workbook."""

from io import BytesIO
import os
from pathlib import Path
import posixpath
import tempfile
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from PIL import Image
from .review_workbook import NS, digest, inspect, make_backup
from .review_sources import CHANNEL_COLUMNS

REL = "http://schemas.openxmlformats.org/package/2006/relationships"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
D = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def image_bytes(path):
    try:
        data = Path(path).read_bytes()
        if len(data) > 40 * 1024 * 1024:
            raise ValueError("截图文件超过40MB，请选择原始PNG或JPEG截图")
        with Image.open(BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG") or min(image.size) < 100:
                raise ValueError("请选择清晰的PNG或JPEG商品页面截图（宽高至少100像素）")
            extension = "png" if image.format == "PNG" else "jpeg"
            image.verify()
        return data, extension
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"无法读取截图：{error}") from error


def _rels(part):
    return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")


def _target(part, target):
    return (
        target.lstrip("/")
        if target.startswith("/")
        else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
    )


def write_evidence(path, expected, month, product, identity, channel, payload, extension, *, basis_slot=None, basis_count=1, backup=True):
    if basis_slot is None and channel not in CHANNEL_COLUMNS:
        raise ValueError("请选择官网、天猫或京东渠道")
    if digest(path, fresh=True) != expected:
        raise ValueError("报价文件已被外部修改，请重新打开本批次")
    rows = inspect(path, month)
    row = rows.get(product.output_row, {})
    if tuple(str(row.get(c, "") or "") for c in ("C", "D", "E", "F", "G")) != identity:
        raise ValueError("商品行定位不一致，已取消截图替换")
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
        changes = {}
        workbook = ET.fromstring(parts["xl/workbook.xml"])
        sheet = next(
            s
            for s in workbook.findall(f"{{{NS}}}sheets/{{{NS}}}sheet")
            if s.get("name") == "5G手机"
        )
        relations = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
        sheet_part = _target(
            "xl/workbook.xml",
            next(r.get("Target") for r in relations if r.get("Id") == sheet.get(f"{{{R}}}id")),
        )
        root = ET.fromstring(parts[sheet_part])
        sheet_rel_part = _rels(sheet_part)
        sheet_rels = (
            ET.fromstring(parts[sheet_rel_part])
            if sheet_rel_part in parts
            else ET.Element(f"{{{REL}}}Relationships")
        )
        reference = root.find(f"{{{NS}}}drawing")
        content_types = ET.fromstring(parts["[Content_Types].xml"])
        if reference is not None:
            drawing_part = _target(
                sheet_part,
                next(
                    r.get("Target")
                    for r in sheet_rels
                    if r.get("Id") == reference.get(f"{{{R}}}id")
                ),
            )
            drawing = ET.fromstring(parts[drawing_part])
        else:
            drawing_part = f"xl/drawings/review-{uuid4().hex}.xml"
            drawing = ET.Element(f"{{{D}}}wsDr")
            rid = "rIdReview" + uuid4().hex
            ET.SubElement(
                sheet_rels,
                f"{{{REL}}}Relationship",
                Id=rid,
                Type=R + "/drawing",
                Target="/" + drawing_part,
            )
            # Insert before later worksheet elements, preserving original worksheet XML bytes.
            import re

            xml = parts[sheet_part].decode()
            element = f'<drawing xmlns:r="{R}" r:id="{rid}"/>'
            match = re.search(
                r"<(?:legacyDrawing|legacyDrawingHF|picture|oleObjects|controls|webPublishItems|tableParts|extLst)\b|</worksheet>",
                xml,
            )
            if match is None:
                raise ValueError("工作表结构不支持安全添加截图")
            changes[sheet_part] = (xml[: match.start()] + element + xml[match.start() :]).encode()
            changes[sheet_rel_part] = ET.tostring(sheet_rels)
            ET.SubElement(
                content_types,
                f"{{{CT}}}Override",
                PartName="/" + drawing_part,
                ContentType="application/vnd.openxmlformats-officedocument.drawingml.drawing+xml",
            )
        drawing_rel_part = _rels(drawing_part)
        drawing_rels = (
            ET.fromstring(parts[drawing_rel_part])
            if drawing_rel_part in parts
            else ET.Element(f"{{{REL}}}Relationships")
        )
        image_col = 40 if basis_slot is not None else CHANNEL_COLUMNS[channel][1]
        picture_name = f"报价依据-{product.output_row}-{basis_slot}" if basis_slot is not None else f"补充截图-{channel}-{product.output_row}"
        anchors = []
        for anchor in drawing:
            start = anchor.find(f"{{{D}}}from")
            if (
                start is not None
                and start.findtext(f"{{{D}}}col") == str(image_col)
                and start.findtext(f"{{{D}}}row") == str(product.output_row - 1)
                and (basis_slot is None or any(n.get("name") == picture_name for n in anchor.iter(f"{{{D}}}cNvPr")))
            ):
                if anchor.find(f"{{{D}}}pic") is None:
                    raise ValueError("目标位置有非图片对象，不能自动覆盖")
                anchors.append(anchor)
        if len(anchors) > 1:
            raise ValueError("同一商品渠道存在多张重叠截图，请先核实工作簿结构")
        if anchors:
            anchor = anchors[0]
            blip = anchor.find(f".//{{{A}}}blip")
            if blip is None:
                raise ValueError("原截图关联结构异常，不能安全替换")
        else:
            anchor = ET.SubElement(drawing, f"{{{D}}}twoCellAnchor", editAs="twoCell")
            for name, col, row_number in [
                ("from", image_col, product.output_row - 1),
                ("to", image_col + 1, product.output_row),
            ]:
                marker = ET.SubElement(anchor, f"{{{D}}}{name}")
                for key, value in [("col", col), ("colOff", 0), ("row", row_number), ("rowOff", 0)]:
                    ET.SubElement(marker, f"{{{D}}}{key}").text = str(value)
            pic = ET.SubElement(anchor, f"{{{D}}}pic")
            nv = ET.SubElement(pic, f"{{{D}}}nvPicPr")
            ids = [int(e.get("id", "0")) for e in drawing.iter(f"{{{D}}}cNvPr")]
            ET.SubElement(
                nv,
                f"{{{D}}}cNvPr",
                id=str(max(ids, default=0) + 1),
                name=picture_name,
            )
            ET.SubElement(nv, f"{{{D}}}cNvPicPr")
            fill = ET.SubElement(pic, f"{{{D}}}blipFill")
            blip = ET.SubElement(fill, f"{{{A}}}blip")
            ET.SubElement(ET.SubElement(fill, f"{{{A}}}stretch"), f"{{{A}}}fillRect")
            shape = ET.SubElement(pic, f"{{{D}}}spPr")
            ET.SubElement(ET.SubElement(shape, f"{{{A}}}prstGeom", prst="rect"), f"{{{A}}}avLst")
            ET.SubElement(anchor, f"{{{D}}}clientData")
        if basis_slot is not None:
            # Each original image has its own drawing, no resampling or composition.
            columns = root.find(f"{{{NS}}}cols")
            width = next((float(c.get('width', '30')) for c in (columns if columns is not None else ()) if int(c.get('min')) <= 41 <= int(c.get('max'))), 30)
            emu_width = int((width * 7 + 5) * 9525)
            for mark, fraction in [('from', basis_slot / basis_count), ('to', (basis_slot + 1) / basis_count)]:
                marker = anchor.find(f"{{{D}}}{mark}")
                marker.find(f"{{{D}}}col").text = str(40 if fraction < 1 else 41)
                marker.find(f"{{{D}}}colOff").text = str(int(emu_width * fraction) if fraction < 1 else 0)
                marker.find(f"{{{D}}}row").text = str(product.output_row - 1 if mark == 'from' else product.output_row)
        media = f"xl/media/review-{uuid4().hex}.{extension}"
        rid = "rIdReview" + uuid4().hex
        blip.set(f"{{{R}}}embed", rid)
        blip.attrib.pop(f"{{{R}}}link", None)
        ET.SubElement(
            drawing_rels, f"{{{REL}}}Relationship", Id=rid, Type=R + "/image", Target="/" + media
        )
        if not any(
            e.tag == f"{{{CT}}}Default" and e.get("Extension") == extension for e in content_types
        ):
            ET.SubElement(
                content_types,
                f"{{{CT}}}Default",
                Extension=extension,
                ContentType="image/" + extension,
            )
        changes.update(
            {
                drawing_part: ET.tostring(drawing),
                drawing_rel_part: ET.tostring(drawing_rels),
                media: payload,
                "[Content_Types].xml": ET.tostring(content_types),
            }
        )
        fd, name = tempfile.mkstemp(prefix=".evidence-", suffix=".xlsx", dir=path.parent)
        os.close(fd)
        try:
            with ZipFile(name, "w") as out:
                out.comment = archive.comment
                for item in archive.infolist():
                    out.writestr(item, changes.get(item.filename, parts[item.filename]))
                for key, value in changes.items():
                    if key not in parts:
                        out.writestr(key, value)
            if inspect(Path(name), month) != rows:
                raise ValueError("换图校验失败：报价单元格发生变化")
            from .review_sources import workbook_channels
            import hashlib

            if basis_slot is None:
                records = workbook_channels(Path(name), rows)
                actual = next((t for t in records if t.source_row_number == product.output_row and t.channel == channel), None)
                if actual is None or digest(actual.evidence_path) != hashlib.sha256(payload).hexdigest():
                    raise ValueError("换图校验失败：图片未准确关联到商品与渠道")
            else:
                from openpyxl import load_workbook
                book = load_workbook(name)
                try:
                    matches = [im for im in book['5G手机']._images if im.anchor._from.col == 40 and im.anchor._from.row == product.output_row - 1]
                    if not any(hashlib.sha256(im._data()).hexdigest() == hashlib.sha256(payload).hexdigest() for im in matches):
                        raise ValueError('依据图片未正确写入AO')
                finally:
                    book.close()
            backup = make_backup(path, expected) if backup else None
            os.replace(name, path)
            return backup
        finally:
            if os.path.exists(name):
                os.unlink(name)

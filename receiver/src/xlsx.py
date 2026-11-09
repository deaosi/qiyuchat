"""最小 XLSX / CSV 读取器，使用 zipfile + ElementTree，不执行表格公式。"""

from __future__ import annotations

import csv
import io
import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    """A1 -> 0, AA1 -> 26"""
    n = 0
    for ch in ref:
        if "A" <= ch.upper() <= "Z":
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def read_xlsx(data: bytes, max_rows: int = 0) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if len(zf.infolist()) > 2000 or sum(i.file_size for i in zf.infolist()) > 64 * 1024 * 1024:
            raise ValueError("XLSX 解压后超过 64 MB 或文件项过多，请拆分文件")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        # 单表导入：读取第一个工作表，不拼接其他工作表。
        sheet_name = next((name for name in sorted(zf.namelist())
                           if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")), None)
        if not sheet_name:
            raise ValueError("XLSX 里找不到工作表")
        root = ET.fromstring(zf.read(sheet_name))
        rows: list[list[str]] = []
        for row in root.iter(f"{NS}row"):
            cells: dict[int, str] = {}
            for c in row.findall(f"{NS}c"):
                ref = c.get("r", "")
                idx = _col_index(ref) if ref else len(cells)
                if not 0 <= idx < 4096:
                    raise ValueError("XLSX 列数过多或单元格坐标无效")
                t, v = c.get("t"), c.find(f"{NS}v")
                if t == "inlineStr":
                    isn = c.find(f"{NS}is")
                    val = "".join(x.text or "" for x in isn.iter(f"{NS}t")) if isn is not None else ""
                elif v is None:
                    val = ""
                elif t == "s":
                    try:
                        val = shared[int(v.text or 0)]
                    except (ValueError, IndexError):
                        val = ""
                else:
                    val = v.text or ""
                cells[idx] = val
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
                if max_rows and len(rows) > max_rows:
                    raise ValueError("一次最多导入 5000 行，请拆分文件")
        return rows


def read_table(data: bytes, max_rows: int = 0) -> list[list[str]]:
    """自动识别 XLSX / CSV，第一行是表头；本地导入可传行数上限。"""
    if data[:2] == b"PK":
        return read_xlsx(data, max_rows=max_rows)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 编码无法识别，请另存为 UTF-8 CSV 后重试")
    if "\x00" in text:
        raise ValueError("文件不是可读取的 CSV/XLSX，请检查格式")
    # 只依据表头判断分隔符，正文中可能有大量逗号或制表符。
    header = text.splitlines()[0] if text else ""
    delim = "\t" if header.count("\t") > header.count(",") else ","
    rows = []
    try:
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delim, strict=True):
            rows.append(row)
            if max_rows and len(rows) > max_rows:
                raise ValueError("一次最多导入 5000 行，请拆分文件")
    except csv.Error:
        raise ValueError("CSV 格式无效或单个字段过长，请检查引号或改用 XLSX") from None
    return rows


def to_dicts(rows: list[list[str]]) -> list[dict[str, str]]:
    """第一行当表头，其余转 dict，去掉表头空白。"""
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    return [{header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
            for r in rows[1:] if any(x.strip() for x in r)]

# -*- coding: utf-8 -*-
"""表格转链接：把 .xlsx / .csv / .txt 里的链接一次性抠干净。

放在服务端做的三个理由：
  1. 超链接单元格（显示"点击查看"、真实地址藏在 hyperlink 里）只有 openpyxl 看得到；
  2. 多工作表要逐个翻，CSV 还可能是 gb18030 编码；
  3. 纯前端解 xlsx 得再塞一个几百 KB 的库。

只读文件内容，不联网，不写盘。
"""
import csv
import io
import os
import re

import pipeline as PL

MAX_BYTES = 12 * 1024 * 1024      # 单个文件上限
MAX_CELLS = 400000                # 防超大表把内存吃光
MAX_URLS = 3000                   # 一次最多送进输入框的条数

TEXT_EXT = {".csv", ".tsv", ".txt", ".md"}
XLSX_EXT = {".xlsx", ".xlsm"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# 没有 http 前缀的 www 写法，补全后当链接用。
# 前面挡掉 @ 和字母数字，免得把 someone@www.x.com、abcwww.x.com 当成链接。
BARE_RE = re.compile(
    r"(?<![@\w.\-/])(?:www\.)[a-z0-9\-]+(?:\.[a-z0-9\-]+)+"
    r"(?:/[^\s<>\"'）】」，。；]*)?",
    re.I)


class SheetError(Exception):
    """读不了的文件，消息直接拿去给用户看。"""


def _decode(blob):
    if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return blob.decode("utf-16", "replace")
    if blob[:3] == b"\xef\xbb\xbf":
        return blob[3:].decode("utf-8", "replace")
    for enc in ("utf-8", "gb18030"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", "replace")


def _csv_cells(text):
    sample = text[:8192]
    delim = ","
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        # Sniffer 对只有一两行的表经常失手，退回按第一行数分隔符
        line = (sample.splitlines() or [""])[0]
        best = max(",;\t|", key=line.count)
        if line.count(best) > line.count(","):
            delim = best
    cells, rows = [], 0
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        rows += 1
        for v in row:
            if isinstance(v, str) and v.strip():
                cells.append(v)
    return cells, rows


def _xlsx_cells(blob):
    try:
        import openpyxl
    except ImportError:
        raise SheetError("服务端没装 openpyxl，读不了 Excel。可以先把表格另存为 CSV 再拖进来。")

    cells, rows, sheets = [], 0, []

    # 小文件走完整模式，这样才能读到 hyperlink（显示文字是标题、地址藏在链接里的那种）
    if len(blob) <= 3 * 1024 * 1024:
        try:
            wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True)
        except Exception as e:
            raise SheetError("Excel 打不开：%s" % e)
        try:
            for ws in wb.worksheets:
                sheets.append(ws.title)
                for row in ws.iter_rows():
                    rows += 1
                    for c in row:
                        v = c.value
                        if isinstance(v, str) and v.strip():
                            cells.append(v)
                        elif not isinstance(v, (int, float)) and v is not None:
                            cells.append(str(v))
                        try:
                            tgt = c.hyperlink.target if c.hyperlink else ""
                        except Exception:
                            tgt = ""
                        if tgt:
                            cells.append(tgt)
                    if len(cells) > MAX_CELLS:
                        break
        finally:
            try:
                wb.close()
            except Exception:
                pass
        return cells, rows, sheets

    # 大文件走只读流式，内存稳一点（代价是读不到 hyperlink，正文里的地址照样能抠）
    try:
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as e:
        raise SheetError("Excel 打不开：%s" % e)
    try:
        for ws in wb.worksheets:
            sheets.append(ws.title)
            for row in ws.iter_rows(values_only=True):
                rows += 1
                for v in row:
                    if isinstance(v, str):
                        if v.strip():
                            cells.append(v)
                    elif v is not None and not isinstance(v, (int, float)):
                        cells.append(str(v))
                if len(cells) > MAX_CELLS:
                    break
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return cells, rows, sheets


def _urls_in(text):
    """一个单元格里可能塞了好几条链接，也可能夹着一堆说明文字。"""
    out = [(u, False) for u in PL.URL_RE.findall(text)]
    out += [(m, True) for m in BARE_RE.findall(text)]
    return out


def parse(name, blob, limit=MAX_URLS):
    """把表格拆成链接列表，附带一份够前端写提示的统计。"""
    if not blob:
        raise SheetError("文件是空的")
    if len(blob) > MAX_BYTES:
        raise SheetError("文件超过 %d MB，先拆成小一点的表" % (MAX_BYTES // 1024 // 1024))

    ext = os.path.splitext(name or "")[1].lower()

    # 先看文件头，别被扩展名骗了：.xls 是 OLE 老格式，.xlsx 其实是 zip
    if blob[:8] == OLE_MAGIC:
        raise SheetError("这是老式 .xls（二进制格式），openpyxl 读不了。"
                         "用 Excel 打开另存为 .xlsx 再拖进来。")
    if ext in XLSX_EXT or (ext not in TEXT_EXT and blob[:2] == b"PK"):
        cells, rows, sheets = _xlsx_cells(blob)
    else:
        cells, rows = _csv_cells(_decode(blob))
        sheets = []

    raw, upgraded = 0, 0
    seen, uniq = set(), []
    for c in cells:
        for u, bare in _urls_in(c):
            raw += 1
            if bare:
                upgraded += 1
                u = "https://" + u
            n = PL.norm_url(u)
            if not n or n in seen:
                continue
            seen.add(n)
            uniq.append(n)

    hosts = []
    for u in uniq:
        h = PL.host_of(u)
        if h and h not in hosts:
            hosts.append(h)

    truncated = len(uniq) > limit
    urls = uniq[:limit]
    return {
        "name": name,
        "urls": urls,
        "count": len(urls),
        "raw": raw,                                   # 命中次数（含重复）
        "dups": max(0, raw - len(uniq)),              # 表内重复
        "upgraded": upgraded,                         # 补全了 https 的 www 写法
        "cells": len(cells),                          # 有内容的单元格数
        "rows": rows,
        "sheets": sheets,
        "hosts": hosts[:24],
        "host_count": len(hosts),
        "truncated": truncated,
    }

# -*- coding: utf-8 -*-
"""把抓取结果渲染为 Markdown 与 Word（每篇一文档）。

Word 部分注意两件事：
1. 手动插入 w:pBdr / w:shd / w:tcW 时必须遵循 OOXML 的固定子元素顺序，否则 Word 会提示"内容有问题"。
2. 超链接要用真实关系 + w:hyperlink，不能只写成一段带下划线的文字。
"""
import os
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

PPR_ORDER = ("w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr",
             "w:widowControl", "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd",
             "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
             "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
             "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
             "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
             "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
             "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange")
TCPR_ORDER = ("w:cnfStyle", "w:tcW", "w:gridSpan", "w:hMerge", "w:vMerge",
              "w:tcBorders", "w:shd", "w:noWrap", "w:tcMar", "w:textDirection",
              "w:tcFitText", "w:vAlign", "w:hideMark")

INK = RGBColor(0x22, 0x22, 0x22)
GREY = RGBColor(0x6B, 0x72, 0x80)
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)


def _insert(parent, el, tag, order):
    idx = order.index(tag)
    parent.insert_element_before(el, *order[idx + 1:])


def _shd(fill):
    e = OxmlElement("w:shd")
    e.set(qn("w:val"), "clear")
    e.set(qn("w:color"), "auto")
    e.set(qn("w:fill"), fill)
    return e


def _border(p, color="D8DEE6", sides=("top", "left", "bottom", "right"), sz="4"):
    pPr = p._p.get_or_add_pPr()
    bd = OxmlElement("w:pBdr")
    for side in sides:
        el = OxmlElement("w:%s" % side)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), sz)
        el.set(qn("w:color"), color)
        el.set(qn("w:space"), "6")
        bd.append(el)
    _insert(pPr, bd, "w:pBdr", PPR_ORDER)


def _font(run, name="Times New Roman", size=None, bold=None, color=None, eastasia=None):
    run.font.name = name
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rPr.insert(0, rf)
    rf.set(qn("w:ascii"), name)
    rf.set(qn("w:hAnsi"), name)
    if eastasia:
        rf.set(qn("w:eastAsia"), eastasia)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    return run


def _link(par, url, text, size=9.5):
    part = par.part
    r_id = part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), r_id)
    run = par.add_run(text)
    _font(run, "Times New Roman", size, None, ACCENT)
    run.font.underline = True
    h.append(run._element)
    par._p.append(h)
    return par


def _rule(doc, color="DCDFE4"):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(6)
    _border(p, color, ("bottom",), "6")
    return p


def _shade(p, fill="F4F6F9"):
    pPr = p._p.get_or_add_pPr()
    _insert(pPr, _shd(fill), "w:shd", PPR_ORDER)


def summary_block(doc, rec):
    """摘要区：左侧强调线 + 浅底，与正文视觉分离。"""
    if not rec.get("summary"):
        return
    lab = doc.add_paragraph()
    lab.paragraph_format.space_before = Pt(4)
    lab.paragraph_format.space_after = Pt(2)
    lab.paragraph_format.keep_with_next = True
    _font(lab.add_run("摘　要"), "Times New Roman", 9.5, True, ACCENT, eastasia="黑体")

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.line_spacing = 1.4
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _border(p, "1F3A5F", ("top", "left", "bottom", "right"), "4")
    _shade(p, "F4F6F9")
    _font(p.add_run(rec["summary"].strip()), "Times New Roman", 10, None, INK,
          eastasia="宋体")

    if rec.get("keywords"):
        q = doc.add_paragraph()
        q.paragraph_format.space_after = Pt(2)
        _font(q.add_run("关键词："), "Times New Roman", 9, True, GREY, eastasia="黑体")
        _font(q.add_run("、".join(rec["keywords"])), "Times New Roman", 9, None, INK,
              eastasia="宋体")

    if rec.get("summary_how"):
        q = doc.add_paragraph()
        q.paragraph_format.space_after = Pt(6)
        _font(q.add_run("摘要方式：%s" % rec["summary_how"]), "Times New Roman", 8.5,
              None, GREY, eastasia="楷体")


def _field(par, instr, size=9, cached="1"):
    """往段落尾部插一个域（PAGE / NUMPAGES）。

    用 begin → instrText → separate → 缓存值 → end 的完整结构：
    fldSimple 在部分阅读器里不刷新，缓存值能保证没打开域更新时也不是空白。
    """
    def _run():
        r = OxmlElement("w:r")
        rp = OxmlElement("w:rPr")
        rf = OxmlElement("w:rFonts")
        rf.set(qn("w:ascii"), "Times New Roman")
        rf.set(qn("w:hAnsi"), "Times New Roman")
        rf.set(qn("w:eastAsia"), "宋体")
        rp.append(rf)
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(int(size * 2)))
        rp.append(sz)
        c = OxmlElement("w:color")
        c.set(qn("w:val"), "6B7280")
        rp.append(c)
        r.append(rp)
        return r

    for kind in ("begin", "instr", "separate", "text", "end"):
        run = _run()
        if kind == "instr":
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = " %s " % instr
        elif kind == "text":
            el = OxmlElement("w:t")
            el.text = cached
        else:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        run.append(el)
        par._p.append(run)


def _footer(doc, left_text=""):
    """页脚只放页码：第 X 页，共 Y 页。left_text 保留参数只为兼容旧调用。"""
    sect = doc.sections[0]
    fp = sect.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fp.paragraph_format.space_before = Pt(0)
    fp.paragraph_format.space_after = Pt(0)

    _font(fp.add_run("第 "), "Times New Roman", 9, None, GREY, eastasia="宋体")
    _field(fp, "PAGE")
    _font(fp.add_run(" 页，共 "), "Times New Roman", 9, None, GREY, eastasia="宋体")
    _field(fp, "NUMPAGES")
    _font(fp.add_run(" 页"), "Times New Roman", 9, None, GREY, eastasia="宋体")


def iter_body(rec):
    """按段吐出 (tag, 原文, 译文)，交给各渲染器决定怎么排。

    translate 模式：off 只有原文；zh 只有译文；both 原文译文都吐，用于对照。
    """
    blocks = rec.get("blocks") or []
    trans = rec.get("trans_blocks") or []
    mode = rec.get("translate") or "off"
    for i, (tag, text) in enumerate(blocks):
        zh = trans[i][1] if i < len(trans) else ""
        if mode == "off":
            yield tag, text, ""
        elif mode == "zh":
            yield tag, "", (zh or text)
        else:
            yield tag, text, (zh or "")


def _head(rec):
    """标题行。翻译过就用中文标题，原题附在后面。"""
    zh = (rec.get("title_zh") or "").strip()
    orig = rec.get("title") or ""
    if zh and zh != orig:
        return zh, orig
    return (orig or "（无标题）"), ""


def render_md(rec):
    head, orig_head = _head(rec)
    L = ["# %s" % head, ""]
    if orig_head:
        L += ["*原文标题：%s*" % orig_head, ""]
    meta = []
    if rec.get("site"):
        meta.append("来源：%s" % rec["site"])
    if rec.get("date"):
        meta.append("发布：%s" % rec["date"])
    if rec.get("author"):
        meta.append("作者：%s" % rec["author"])
    meta.append("正文字符：%d" % rec.get("chars", 0))
    L.append("- " + "　|　".join(meta))
    L.append("- 原文：%s" % rec["url"])
    L.append("")
    if rec.get("summary"):
        L += ["## 摘要", "", rec["summary"].strip(), ""]
        if rec.get("keywords"):
            L += ["**关键词**：" + "、".join(rec["keywords"]), ""]
        L += ["> 摘要方式：%s" % (rec.get("summary_how") or "本地提取式"), ""]
    if rec.get("trans_how") and rec.get("translate") != "off":
        L += ["> 翻译：%s" % rec["trans_how"], ""]

    for tag, orig, zh in iter_body(rec):
        if orig:
            L += _md_block(tag, orig, quote=(rec.get("translate") == "both"))
        if zh:
            L += _md_block(tag, zh)
    return "\n".join(L).rstrip() + "\n"


def _md_block(tag, text, quote=False):
    if quote:
        return ["> " + text, ""]
    if tag in ("h1", "h2"):
        return ["## " + text, ""]
    if tag == "h3":
        return ["### " + text, ""]
    if tag == "h4":
        return ["#### " + text, ""]
    if tag == "li":
        return ["- " + text]
    if tag == "blockquote":
        return ["> " + text, ""]
    return [text, ""]


def _tidy(text):
    """收拾段落里的零碎：去掉行尾空格，连续空行压成一个。

    只压"连续"空行，单个空行留着——代码块里的空行是内容的一部分。
    """
    out = []
    for ln in text.strip().splitlines():
        ln = ln.rstrip()
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    return "\n".join(out)


def _emit(doc, tag, text, secondary=False):
    """排一段正文。secondary=True 用于双语对照里的原文，缩小变灰以便区分。"""
    text = _tidy(text)
    # \n 会被 python-docx 转成 <w:br/>，而两端对齐会把软换行那行拉满，
    # 出现字间距巨大的断句。带软换行的段落改左对齐，行宽自然、断行保持原样。
    align = WD_ALIGN_PARAGRAPH.LEFT if "\n" in text else WD_ALIGN_PARAGRAPH.JUSTIFY
    if tag in ("h1", "h2"):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(9 if secondary else 12)
        p.paragraph_format.space_after = Pt(2 if secondary else 4)
        p.paragraph_format.keep_with_next = True
        if secondary:
            _font(p.add_run(text), "Times New Roman", 9.5, False, GREY, eastasia="楷体")
        else:
            _font(p.add_run(text), "Times New Roman", 12.5, True, ACCENT, eastasia="黑体")
    elif tag == "h3":
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(7 if secondary else 9)
        p.paragraph_format.space_after = Pt(2 if secondary else 3)
        p.paragraph_format.keep_with_next = True
        if secondary:
            _font(p.add_run(text), "Times New Roman", 9.5, False, GREY, eastasia="楷体")
        else:
            _font(p.add_run(text), "Times New Roman", 11, True, INK, eastasia="黑体")
    elif tag == "h4":
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(5 if secondary else 7)
        p.paragraph_format.space_after = Pt(2)
        _font(p.add_run(text), "Times New Roman", 9.5 if secondary else 10.5,
              not secondary, GREY if secondary else INK,
              eastasia="楷体" if secondary else "黑体")
    elif tag == "li":
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.3
        if secondary:
            _font(p.add_run(text), "Times New Roman", 9, False, GREY, eastasia="楷体")
        else:
            _font(p.add_run(text), "Times New Roman", 10.5, None, INK, eastasia="宋体")
    elif tag == "blockquote":
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.8)
        p.paragraph_format.right_indent = Cm(0.5)
        p.paragraph_format.space_after = Pt(5)
        _border(p, "C9CFDA", ("left",), "18")
        _font(p.add_run(text), "Times New Roman", 10, False, RGBColor(0x44, 0x4A, 0x55),
              eastasia="楷体")
    else:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        if secondary:
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.3
            p.alignment = align
            _font(p.add_run(text), "Times New Roman", 9, False, GREY, eastasia="楷体")
        else:
            p.alignment = align
            _font(p.add_run(text), "Times New Roman", 10.5, None, INK, eastasia="宋体")


def render_docx(rec, path, index=0, total=0, scraped_at="", note=""):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    for attr, v in (("top_margin", 2.4), ("bottom_margin", 2.2),
                    ("left_margin", 2.6), ("right_margin", 2.4)):
        setattr(sec, attr, Cm(v))

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.35
    normal.paragraph_format.space_after = Pt(6)

    # 不要页眉：连 header 部件都不创建，Word 里打开就是空的
    # （只要不去访问 sec.header，python-docx 就不会生成 header1.xml）

    # 标题
    head, orig_head = _head(rec)
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2 if orig_head else 10)
    _font(p.add_run(head), "Times New Roman", 17, True, INK, eastasia="黑体")
    if orig_head:
        q = doc.add_paragraph()
        q.paragraph_format.space_after = Pt(10)
        _font(q.add_run(orig_head), "Times New Roman", 10, False, GREY, eastasia="楷体")

    # 元信息
    for label, val in (("来源", rec.get("site", "")), ("发布", rec.get("date", "")),
                       ("作者", rec.get("author", "")),
                       ("正文", "%d 字符" % rec.get("chars", 0))):
        if not val:
            continue
        q = doc.add_paragraph()
        q.paragraph_format.space_after = Pt(1)
        _font(q.add_run("%s：" % label), "Times New Roman", 9, True, GREY, eastasia="黑体")
        _font(q.add_run(str(val)), "Times New Roman", 9, None, INK, eastasia="宋体")

    q = doc.add_paragraph()
    q.paragraph_format.space_after = Pt(1)
    _font(q.add_run("原文："), "Times New Roman", 9, True, GREY, eastasia="黑体")
    _link(q, rec["url"], rec["url"] if len(rec["url"]) < 95 else rec["url"][:92] + "...",
          8.5)

    _rule(doc)

    summary_block(doc, rec)

    if rec.get("trans_how") and rec.get("translate") != "off":
        note = (note + "　·　" if note else "") + "翻译：" + rec["trans_how"]
    if note:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(8)
        _font(p.add_run(note), "Times New Roman", 8.5, None, GREY, eastasia="楷体")

    # 只有"中英对照"模式才把原文降格成小灰字；不翻译时它就是正文，得用正文字号
    both = (rec.get("translate") or "off") == "both"
    for tag, orig, zh in iter_body(rec):
        if orig:
            _emit(doc, tag, orig, secondary=both)
        if zh:
            _emit(doc, tag, zh)

    _rule(doc)
    end = doc.add_paragraph()
    _font(end.add_run("抓取时间 %s　·　正文由 %s 原文转录，版权归原作者与发布机构所有，"
                      "仅供研究、教学与语料分析使用。" % (scraped_at, rec.get("site") or "网站")),
          "Times New Roman", 8, None, GREY, eastasia="楷体")

    _footer(doc)
    doc.save(path)
    return path


def safe_name(rec, index):
    t = rec.get("title") or "untitled"
    t = re.sub(r"[\\/:*?\"<>|\r\n\t]", "", t)
    t = re.sub(r"\s+", "-", t.strip())[:60]
    site = re.sub(r"[^\w\-]", "", (rec.get("site") or "site"))[:14]
    return "%03d_%s_%s_%s.docx" % (index, site, rec.get("date") or "nodate", t)

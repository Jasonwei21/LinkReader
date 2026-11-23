# -*- coding: utf-8 -*-
"""一条链接的完整流水线：抓正文 → 生成摘要 → 落盘（txt / md / docx / 汇总表）。"""
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import core_fetch as LF
import docx_out as DX
import ocr_win as OW
import render_fetch as RF
import summarizer as SZ
import translator as TR

URL_RE = re.compile(r"https?://[^\s<>\"'）】」，。；]+", re.I)
HOST_SEM = {}
HOST_LOCK = threading.Lock()


def norm_url(u):
    u = u.strip().strip(".,;)]}\"'")
    u = re.sub(r"[?&](utm_[^=&]+|ref|spm|from|share_token)=[^&]*", "", u)
    if re.search(r"[?&]$", u):
        u = u[:-1]
    return u


def host_of(u):
    m = re.match(r"https?://([^/]+)", u)
    return (m.group(1).lower().replace("www.", "") if m else "")


def extract_urls(text):
    """从一段文字（可能粘了一堆链接和标题）里抠出链接并去重。"""
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        n = norm_url(u)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _slot(host, n=2):
    with HOST_LOCK:
        if host not in HOST_SEM:
            HOST_SEM[host] = threading.Semaphore(n)
        return HOST_SEM[host]


def _fallback_blocks(raw):
    """<p> 标签不规范的站点兜底：按 DOM 打分找正文容器。"""
    root = LF.parse_html(raw)
    best, best_score = root, -1
    for n in root.walk():
        if n.tag not in ("article", "main", "section", "div"):
            continue
        if LF._junk_node(n):
            continue
        s = 0
        for d in n.walk():
            if d.tag in ("p", "blockquote", "li"):
                t = LF.p_text(d)
                if len(t) >= 40:
                    s += len(t)
        if s > best_score:
            best, best_score = n, s
    out = []
    for d in best.walk():
        if d.tag in ("p", "blockquote"):
            t = LF.p_text(d)
            if len(t) >= 40:
                out.append([d.tag, t])
    return out


def _pdf_sidecar(raw, blocks, url, cache_dir):
    links = LF.find_pdf_links(raw, url)
    for u in links:
        try:
            data = LF.fetch_bytes(u, cache_dir)
        except Exception:
            continue
        pb, _ = LF.pdf_blocks(data)
        if not pb:
            continue
        txt = " ".join(t for _, t in pb)
        ok, hit, tot = LF.lead_match(blocks, txt)
        if ok and len(txt) > sum(len(t) for _, t in blocks) * 1.5:
            return pb, "正文取自同站 PDF 附件（首段比对 %d/%d）" % (hit, tot)
    return None, ""


def _analyze(raw, url, cache_dir, pdf_fallback=True, status=None):
    """解析一段 HTML：标题、正文块、订阅墙判定。返回 dict。

    抽成函数是为了让"原始抓取"和"浏览器渲染"两段 HTML 能走同一套判定，
    尤其是 detect_wall 必须对渲染后的 HTML 再跑一次。
    """
    meta = LF.meta_of(raw, url)
    title = meta["title"] or ""
    blocks = LF.blocks_from_html(raw, title, junk=LF.junk_texts(raw))
    chars = sum(len(t) for _, t in blocks)

    if chars < 400:
        alt = _fallback_blocks(raw)
        if sum(len(t) for _, t in alt) > chars:
            blocks = alt
            chars = sum(len(t) for _, t in alt)

    wall, kind, ev = LF.detect_wall(raw, meta, blocks, status=status)

    pdf_note = ""
    if not wall and chars < 2500 and pdf_fallback and not url.lower().endswith(".pdf"):
        pb, how = _pdf_sidecar(raw, blocks, url, cache_dir)
        if pb:
            blocks = pb
            chars = sum(len(t) for _, t in blocks)
            pdf_note = how

    return {
        "meta": meta, "title": title, "blocks": blocks, "chars": chars,
        "wall": wall, "kind": kind, "ev": ev, "pdf_note": pdf_note,
    }


def fetch_one(url, cache_dir, timeout=40, min_chars=400, pdf_fallback=True,
              strict=True, log=None, render_fallback=True, ocr_fallback=True):
    """抓一条。返回 rec（ok=True 表示可用）。

    三级降级：
        1. 直接 HTTP 抓取（最快，正文最准）
        2. 失败于技术性阻碍（超时 / 403 / 429 / JS 空壳）-> 本机浏览器渲染后取 DOM
        3. 渲染后仍抽不出正文（canvas 绘字、字体混淆）-> 截图 + 系统 OCR
    订阅墙/付费墙一律不进入降级：识别到就标记跳过，这是硬边界。
    """
    rec = {
        "url": url, "ok": False, "status": "", "reason": "", "wall": False,
        "title": "", "author": "", "site": "", "date": "", "lang": "",
        "chars": 0, "n_blocks": 0, "blocks": [], "from_pdf": False, "http": None,
        "via": "http", "needs_review": False, "shot": "", "render_note": "",
    }
    say = log or (lambda s: None)
    page_raw = ""
    render_note = ""

    sem = _slot(host_of(url))
    with sem:
        raw, info = LF.fetch(url, cache_dir, timeout=timeout)
        rec["http"] = info.get("status")
        page_raw = raw or ""
        parsed = _analyze(page_raw, url, cache_dir, pdf_fallback, info.get("status")) if raw else None

        # ---- 判断要不要降级：只认技术性阻碍
        need, why = False, ""
        if parsed is None:
            need, why = True, info.get("note") or "无响应"
        elif parsed["wall"]:
            need = False                                  # 订阅墙：绝不渲染
        elif info.get("status") in (403, 429):
            need, why = True, "HTTP %s" % info["status"]
        elif parsed["chars"] < min_chars and len(page_raw) > 5000:
            need, why = True, "正文仅 %d 字符" % parsed["chars"]

        if need and render_fallback and not url.lower().endswith(".pdf"):
            say("    [兜底] %s → 改用浏览器渲染" % why)
            rr = RF.render(url, cache_dir, want_shot=ocr_fallback,
                           timeout=max(timeout, 45), log=say)

            if rr["raw"]:
                p2 = _analyze(rr["raw"], url, cache_dir, pdf_fallback, 200)
                if p2["wall"]:
                    parsed = p2                           # 渲染后才看清是墙，照样按墙处理
                    rec["via"] = "render"
                elif parsed is None or p2["chars"] > parsed["chars"]:
                    parsed = p2
                    page_raw = rr["raw"]
                    rec["via"] = "render"
                    rec["http"] = 200

            # ---- 渲染出 HTML 仍拿不到正文，才上 OCR
            if ocr_fallback and rr["shot"]:
                best = parsed["chars"] if parsed else 0
                if best < min_chars:
                    orc = OW.ocr_image(rr["shot"])
                    ob = OW.blocks_from_ocr(orc, title=(parsed or {}).get("title", ""))
                    oc = sum(len(t) for _, t in ob)
                    if oc > best:
                        base = parsed or {"meta": {}, "title": "", "wall": False,
                                          "kind": "", "ev": "", "pdf_note": ""}
                        parsed = dict(base, blocks=ob, chars=oc, wall=False)
                        rec["via"] = "ocr"
                        rec["shot"] = rr["shot"]
                        rec["needs_review"] = True
                        page_lang = LF.guess_lang(" ".join(t for _, t in ob)[:3000])
                        render_note = OW.quality_note(orc, page_lang)
                        say("    [兜底] 截图 OCR 取到 %d 字符（%s）" % (oc, orc.get("lang") or "?"))

            if parsed is None or parsed["chars"] < min_chars:
                render_note = render_note or rr.get("note", "")

    rec["render_note"] = render_note
    if parsed["pdf_note"]:
        rec["from_pdf"] = True

    if parsed is None:
        rec["status"] = "失败"
        rec["reason"] = "抓取失败：%s" % (info.get("note") or "无响应")
        if render_note:
            rec["reason"] += "；渲染也没成功：%s" % render_note
        return rec

    body = " ".join(t for _, t in parsed["blocks"])
    meta = parsed["meta"]
    rec.update({
        "title": parsed["title"], "author": meta.get("author", ""),
        "site": meta.get("site") or host_of(url), "date": meta.get("date", ""),
        "lang": LF.guess_lang(body or LF.visible_text(page_raw)[:4000]),
        "chars": parsed["chars"], "n_blocks": len(parsed["blocks"]),
        "blocks": parsed["blocks"], "wall": bool(parsed["wall"]),
    })

    if parsed["wall"] and strict:
        rec["status"] = "订阅墙"
        rec["reason"] = "订阅墙（%s），未抓取" % parsed["kind"]
        return rec
    if parsed["chars"] < min_chars:
        rec["status"] = "过短"
        rec["reason"] = "正文仅 %d 字符，可能是栏目页或加载失败" % parsed["chars"]
        if info.get("status") in (403, 429) and rec["via"] == "http":
            rec["status"] = "拦截"
            rec["reason"] = "反爬拦截 HTTP %s，浏览器渲染也没取到正文" % info["status"]
        return rec

    rec["ok"] = True
    rec["status"] = "已抓取"

    if parsed["wall"]:
        rec["reason"] = "订阅墙（%s），按宽松模式保留" % parsed["kind"]
    elif rec["via"] == "ocr":
        rec["reason"] = rec["render_note"] or "OCR 识别，需校对"
    elif rec["via"] == "render":
        rec["reason"] = "公开可读（浏览器渲染取得）"
    elif parsed["pdf_note"]:
        rec["reason"] = parsed["pdf_note"]
    else:
        rec["reason"] = "公开可读" + ("（PDF 全文）" if rec["from_pdf"] else "")
    return rec


def render_txt(rec):
    head, orig_head = DX._head(rec)
    L = [head, ""]
    if orig_head:
        L.append("原文标题：%s" % orig_head)
    meta = [x for x in (rec.get("site"), rec.get("date"), rec.get("author")) if x]
    if meta:
        L.append("　|　".join(meta))
    L.append("原文：%s" % rec["url"])
    L.append("正文字符：%d" % rec.get("chars", 0))
    L.append("")
    if rec.get("summary"):
        L += ["【摘要】", rec["summary"].strip(), ""]
        if rec.get("keywords"):
            L.append("【关键词】" + "、".join(rec["keywords"]))
        L += ["（摘要方式：%s）" % (rec.get("summary_how") or ""), ""]
    mode = rec.get("translate") or "off"
    if rec.get("trans_how") and mode != "off":
        L += ["（翻译：%s）" % rec["trans_how"], ""]

    L += ["【正文】", ""]
    for tag, orig, zh in DX.iter_body(rec):
        if orig:
            if mode == "both":
                L += ["[原] " + orig, ""]
            else:
                L += [_txt_para(tag, orig), ""]
        if zh:
            if mode == "both":
                L += ["[译] " + zh, ""]
            else:
                L += [_txt_para(tag, zh), ""]
    return "\n".join(L).rstrip() + "\n"


def _txt_para(tag, text):
    if tag in ("h1", "h2", "h3", "h4"):
        return "\n" + text + "\n"
    if tag == "li":
        return "- " + text
    return text


def base_name(rec, idx):
    t = rec.get("title") or "untitled"
    t = re.sub(r"[\\/:*?\"<>|\r\n\t]", "", t)
    t = re.sub(r"\s+", "-", t.strip())[:60]
    site = re.sub(r"[^\w\-]", "", (rec.get("site") or "site"))[:14]
    return "%03d_%s_%s_%s" % (idx, site or "site", rec.get("date") or "nodate", t)


def write_xlsx(rows, path):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "摘要汇总"
    head = ["序号", "标题", "中文标题", "来源", "发布日期", "正文字符", "摘要", "关键词",
            "摘要方式", "翻译", "原文链接"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
        c.alignment = Alignment(horizontal="center", vertical="center")
    for i, r in enumerate(rows, 1):
        ws.append([i, r.get("title"), r.get("title_zh") if r.get("translate") != "off" else "",
                   r.get("site"), r.get("date"), r.get("chars"),
                   r.get("summary"), "、".join(r.get("keywords") or []),
                   r.get("summary_how"),
                   (r.get("trans_how") or "—") if r.get("translate") != "off" else "—",
                   r["url"]])
    widths = [6, 40, 40, 16, 12, 10, 60, 24, 18, 22, 38]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=c.column in (2, 3, 7, 8, 11))
    ws.freeze_panes = "A2"
    wb.save(path)


# ---------------------------------------------------------------- 任务
class Job(object):
    def __init__(self, urls, out_root, opt):
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.urls = urls
        self.opt = opt
        self.dir = os.path.join(out_root, self.id)
        self.cache = os.path.join(out_root, "_cache")
        self.state = "running"
        self.msg = ""
        self.items = [{"idx": i + 1, "url": u, "status": "排队中", "title": "",
                       "chars": 0, "summary": "", "reason": "", "file": "",
                       "translated": False, "site": "", "date": "",
                       "keywords": [], "summary_how": "", "docx_file": "",
                       "via": "http", "needs_review": False}
                      for i, u in enumerate(urls)]
        self.done = 0
        self.t0 = time.time()
        self.t_end = None
        self.lock = threading.Lock()
        self.zpath = None

    # -- 内部
    def _set(self, idx, **kw):
        with self.lock:
            self.items[idx - 1].update(kw)

    def snapshot(self):
        with self.lock:
            # 跑完之后用时定住，别让它一直往上涨
            end = self.t_end or time.time()
            return {
                "id": self.id,
                "state": self.state,
                "msg": self.msg,
                "done": self.done,
                "total": len(self.urls),
                "elapsed": round(end - self.t0, 1),
                "items": [dict(x) for x in self.items],
                "zip": os.path.basename(self.zpath) if self.zpath else "",
            }

    def run(self):
        try:
            self._run()
        except Exception:
            self.state = "error"
            self.msg = "任务异常：" + traceback.format_exc(limit=2).splitlines()[-1]
        finally:
            self.t_end = time.time()
            if self.state == "running":
                self.state = "done"

    def _run(self):
        os.makedirs(os.path.join(self.dir, "txt"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "md"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "docx"), exist_ok=True)
        scraped_at = time.strftime("%Y-%m-%d %H:%M")
        workers = max(1, min(6, int(self.opt.get("workers") or 3)))
        results = {}

        def work(pair):
            i, url = pair
            self._set(i, status="抓取中")
            rec = fetch_one(url, self.cache,
                            timeout=int(self.opt.get("timeout") or 40),
                            min_chars=int(self.opt.get("min_chars") or 400),
                            pdf_fallback=bool(self.opt.get("pdf_fallback", True)),
                            strict=bool(self.opt.get("strict", True)),
                            render_fallback=bool(self.opt.get("render_fallback", True)),
                            ocr_fallback=bool(self.opt.get("ocr_fallback", True)),
                            log=getattr(self, "log", None))
            with self.lock:
                results[i] = rec
            if not rec["ok"]:
                self._set(i, status=rec["status"], reason=rec["reason"],
                          title=rec.get("title") or "", chars=rec.get("chars", 0),
                          via=rec.get("via", "http"),
                          needs_review=bool(rec.get("needs_review")))
            else:
                self._set(i, status="已抓取", title=rec["title"], chars=rec["chars"],
                          reason=rec["reason"], via=rec.get("via", "http"),
                          needs_review=bool(rec.get("needs_review")))
            with self.lock:
                self.done += 1
            return rec

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, list(enumerate(self.urls, 1))))

        good = [(i, results[i]) for i in sorted(results) if results[i]["ok"]]
        if not good:
            self.state = "empty"
            self.msg = "没有一条抓取成功"
            self._save_json(results, scraped_at)
            return

        # 摘要 / 翻译
        cfg = self.opt.get("llm") or {}
        mode = self.opt.get("translate") or "off"
        for i, rec in good:
            rec["translate"] = mode
            if self.opt.get("summary", True):
                self._set(i, status="生成摘要")
                try:
                    s = SZ.summarize(rec["blocks"], rec["title"], cfg)
                except Exception as e:
                    s = {"summary": "", "keywords": [],
                         "summary_how": "摘要失败：%s" % e, "engine": "none"}
                rec.update(s)
                self._set(i, summary=s.get("summary", ""),
                          keywords=s.get("keywords") or [],
                          summary_how=s.get("summary_how") or "")
            else:
                rec.update({"summary": "", "keywords": [], "summary_how": ""})

            if mode != "off":
                self._set(i, status="翻译中")
                try:
                    trans, tzh, how = TR.translate(
                        rec["blocks"], cfg, rec["title"],
                        on_progress=lambda d, t, i=i: self._set(
                            i, status="翻译中 %d/%d" % (d, t)))
                    rec["trans_blocks"] = trans
                    rec["title_zh"] = tzh
                    rec["trans_how"] = how
                    self._set(i, translated=True)
                except Exception as e:
                    rec["translate"] = "off"
                    rec["trans_how"] = ""
                    rec["reason"] = (rec.get("reason") or "") + \
                                    "；翻译失败改出原文：%s" % str(e)[:80]
                    self._set(i, translated=False, reason=rec["reason"])

        # 落盘
        for i, rec in good:
            self._set(i, status="写入文件")
        idx_lines = ["# 抓取结果索引", "",
                     "共 %d 篇（%s）" % (len(good), scraped_at), ""]
        rows = []
        for i, rec in good:
            name = base_name(rec, i)
            p_txt = os.path.join(self.dir, "txt", name + ".txt")
            p_md = os.path.join(self.dir, "md", name + ".md")
            p_docx = os.path.join(self.dir, "docx", name + ".docx")
            with open(p_txt, "w", encoding="utf-8") as f:
                f.write(render_txt(rec))
            with open(p_md, "w", encoding="utf-8") as f:
                f.write(DX.render_md(rec))
            note = rec.get("reason") if rec.get("reason") != "公开可读" else ""
            try:
                DX.render_docx(rec, p_docx, index=i, total=len(self.urls),
                               scraped_at=scraped_at, note=note)
                has_docx = True
            except Exception as e:
                has_docx = False
                rec["summary_how"] = (rec.get("summary_how") or "") + "；Word 生成失败：%s" % e
            self._set(i, status="完成",
                      file=os.path.relpath(p_txt, self.dir).replace("\\", "/"),
                      docx=os.path.basename(p_docx) if has_docx else "",
                      docx_file=("docx/%s.docx" % name) if has_docx else "",
                      site=rec.get("site") or "", date=rec.get("date") or "")
            idx_lines.append("%d. %s — %s %s · %d 字符 · 摘要 %d 字" % (
                i, rec["title"][:70] or rec["url"], rec.get("site"),
                rec.get("date") or "无日期", rec["chars"], len(rec.get("summary") or "")))
            idx_lines.append("   - [纯文本](txt/%s.txt) · [Markdown](md/%s.md)%s" % (
                name, name, " · [Word](docx/%s.docx)" % name if has_docx else ""))
            idx_lines.append("   - 摘要：%s" % (rec.get("summary") or "（无）"))
            if rec.get("translate") not in (None, "off"):
                idx_lines.append("   - 翻译：%s（%s）" % (
                    "中文" if rec["translate"] == "zh" else "中英对照",
                    rec.get("trans_how") or ""))
            idx_lines.append("")
            rows.append(rec)

        with open(os.path.join(self.dir, "00_索引.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(idx_lines) + "\n")
        write_xlsx(rows, os.path.join(self.dir, "摘要汇总.xlsx"))
        self._save_json(results, scraped_at)

        # 打包
        import zipfile
        zp = os.path.join(self.dir, "结果打包.zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(self.dir):
                for fn in files:
                    if fn == "结果打包.zip":
                        continue
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, self.dir))
        self.zpath = zp
        self.msg = "完成 %d 篇，剔除 %d 条" % (len(good), len(self.urls) - len(good))

    def _save_json(self, results, scraped_at):
        import json
        out = []
        for i in sorted(results):
            r = dict(results[i])
            r["blocks"] = [[t, s] for t, s in r.get("blocks", [])]
            out.append(r)
        with open(os.path.join(self.dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"urls": self.urls, "scraped_at": scraped_at, "items": out},
                      f, ensure_ascii=False, indent=1)

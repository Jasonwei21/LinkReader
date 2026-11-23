# -*- coding: utf-8 -*-
"""装到新电脑后先跑这个，确认能不能用。"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OK, BAD = [], []


def chk(name, fn):
    try:
        msg = fn()
        OK.append((name, msg or ""))
    except Exception as e:
        BAD.append((name, str(e)))


def need(mod):
    m = importlib.import_module(mod)
    return getattr(m, "__version__", "")


def files():
    need_files = ["app.py", "launch.py", "pipeline.py", "summarizer.py",
                  "translator.py", "llm.py", "core_fetch.py", "docx_out.py",
                  "sheet_links.py", "render_fetch.py", "ocr_win.py",
                  "visible_fetch.py",
                  "static/index.html", "static/style.css", "static/app.js",
                  "static/guide.md", "static/favicon.ico", "static/icon-mark.svg",
                  "static/vendor/vue.global.prod.js",
                  "static/vendor/arco-vue.min.js",
                  "static/vendor/arco.min.css",
                  "static/vendor/marked.min.js"]
    miss = [f for f in need_files if not os.path.exists(os.path.join(HERE, f))]
    if miss:
        raise RuntimeError("缺少文件：" + "、".join(miss))
    return "%d 个文件齐全" % len(need_files)


def vendor_test():
    """前端靠本地 vendor 跑，缺了会白屏，所以要按大小核一遍。"""
    pairs = [("static/vendor/vue.global.prod.js", 80000, "Vue 3"),
             ("static/vendor/arco-vue.min.js", 800000, "Arco Design Vue"),
             ("static/vendor/arco.min.css", 250000, "Arco 样式"),
             ("static/vendor/marked.min.js", 20000, "marked 解析器")]
    out = []
    for rel, low, name in pairs:
        p = os.path.join(HERE, rel)
        n = os.path.getsize(p) if os.path.exists(p) else 0
        if n < low:
            raise RuntimeError("%s 缺失或损坏（%d 字节）" % (name, n))
        out.append("%s %.0fKB" % (name, n / 1024.0))
    return "，".join(out)


def render_test():
    """渲染兜底靠本机浏览器。缺了不算失败——只是这一级降级失效，其余功能照常。"""
    import render_fetch as RF
    exe, name = RF.find_browser()
    if not exe:
        return "未找到 Edge / Chrome（可见窗口兜底失效，其余功能不受影响）"
    return "%s 可用" % name


def html_refs():
    """页面里引用的每个 /static/ 路径都必须真实存在，防打包漏文件。"""
    import re
    p = os.path.join(HERE, "static", "index.html")
    with open(p, encoding="utf-8") as f:
        html = f.read()
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    if not refs:
        raise RuntimeError("index.html 里没找到静态资源引用")
    miss = []
    for r in refs:
        real = os.path.join(HERE, r.lstrip("/").replace("/", os.sep))
        if not os.path.isfile(real):
            miss.append(r)
    if miss:
        raise RuntimeError("页面引用了不存在的文件：" + "、".join(miss))
    return "%d 个引用全部存在" % len(refs)


def guide_test():
    """使用指南是纯前端渲染的，至少要保证分节够多、目录能生成。"""
    import re
    p = os.path.join(HERE, "static", "guide.md")
    with open(p, encoding="utf-8") as f:
        md = f.read()
    h2 = re.findall(r"^## ", md, re.M)
    h3 = re.findall(r"^### ", md, re.M)
    if len(h2) < 8 or len(h3) < 3:
        raise RuntimeError("指南分节太少（h2 %d / h3 %d），目录会很难看"
                           % (len(h2), len(h3)))
    if not md.startswith("# "):
        raise RuntimeError("指南开头缺一级标题")
    return "%d 节 + %d 小节，%d 字" % (len(h2), len(h3), len(md))


def summary_test():
    import summarizer as SZ
    txt = ("中国新能源汽车出口在今年前三季度同比增长百分之三十，欧洲市场占比接近四成。"
           "欧盟对中国电动车加征关税后，多家车企宣布加快本地化建厂，以规避贸易壁垒。"
           "分析人士指出，短期价格优势仍然明显，但供应链本地化将推高成本。"
           "与此同时，碳边境调节机制的实施使电池碳足迹核算成为新的门槛。")
    s, k = SZ.local_summary([["p", txt]], "中国电动车出口")
    if not s or len(s) < 40:
        raise RuntimeError("摘要结果过短：%r" % s)
    if not k:
        raise RuntimeError("关键词为空")
    return "摘要 %d 字，关键词 %d 个" % (len(s), len(k))


def en_summary_test():
    import summarizer as SZ
    txt = ("The European Commission opened a formal investigation into subsidies for "
           "Chinese electric vehicles in October, a move that could lead to tariffs of "
           "up to 25 percent. Analysts expect the probe to take nine months and to cover "
           "battery electric cars shipped from China. Brussels argues that state support "
           "has kept prices artificially low. Beijing said the complaint lacks evidence "
           "and warned of retaliation against European exports.")
    s, k = SZ.local_summary([["p", txt]], "EU opens probe")
    if not s:
        raise RuntimeError("英文摘要为空")
    return "%d 字" % len(s)


def docx_test():
    import tempfile
    import docx_out as DX
    rec = {"url": "https://example.com/a", "title": "测试文档", "site": "example.com",
           "date": "2026-11-18", "author": "", "chars": 120,
           "summary": "这是一段用于验证 Word 生成是否正常的中文摘要，包含若干句子。",
           "keywords": ["测试", "验证"], "summary_how": "本地提取式",
           "blocks": [["p", "这是正文第一段，用来确认段落渲染正常。"],
                      ["h2", "小标题"], ["li", "列表项一"]]}
    p = os.path.join(tempfile.gettempdir(), "_lr_check.docx")
    DX.render_docx(rec, p, 1, 1, "2026-11-18 21:00")
    n = os.path.getsize(p)
    os.remove(p)
    if n < 5000:
        raise RuntimeError("生成的 docx 过小：%d 字节" % n)
    return "%d 字节" % n


def xlsx_test():
    import tempfile
    import pipeline as PL
    p = os.path.join(tempfile.gettempdir(), "_lr_check.xlsx")
    PL.write_xlsx([{"url": "https://a.com", "title": "标题", "site": "a.com",
                    "date": "2026-11-18", "chars": 100, "summary": "摘要",
                    "keywords": ["k1"], "summary_how": "本地提取式"}], p)
    n = os.path.getsize(p)
    os.remove(p)
    return "%d 字节" % n


def fetch_test():
    import tempfile
    import pipeline as PL
    # 用 IANA 的示例域名说明页，比 example.com 正文长，能真正走完抽取流程
    rec = PL.fetch_one("https://www.iana.org/help/example-domains",
                       os.path.join(tempfile.gettempdir(), "_lr_cache"))
    if not rec["blocks"]:
        raise RuntimeError(rec["reason"] or "无内容")
    return "%d 字符" % rec["chars"]


def translate_test():
    """不打真实接口：把 llm.chat 换掉，只验分段对齐和解析。"""
    import json
    import llm
    import translator as TR

    def fake(cfg, messages, **kw):
        arr = json.loads(messages[-1]["content"])
        return json.dumps(["译%d" % (i + 1) for i, _ in enumerate(arr)], ensure_ascii=False)

    orig = llm.chat
    llm.chat = fake
    try:
        out, tzh, note = TR.translate(
            [["p", "First paragraph here."], ["p", "Second paragraph here."]],
            {"base_url": "http://x", "api_key": "k", "model": "m"}, "Some Title")
    finally:
        llm.chat = orig
    if len(out) != 2 or out[0][1] != "译1" or out[1][1] != "译2":
        raise RuntimeError("段落没有一一对齐：%r" % (out,))
    if not tzh:
        raise RuntimeError("标题没有翻译")

    # 接口全挂时必须明确报错，不能伪装成成功
    def boom(cfg, messages, **kw):
        raise llm.LLMError("模拟失败")

    llm.chat = boom
    try:
        TR.translate([["p", "Hello world again."]],
                     {"base_url": "http://x", "api_key": "k", "model": "m"}, "T")
    except llm.LLMError:
        pass
    else:
        raise RuntimeError("接口失败时没有抛错")
    finally:
        llm.chat = orig
    return "2 段对齐，失败能报错"


def main():
    if sys.version_info < (3, 9):
        print("Python 版本过低：%s，需要 3.9 以上" % sys.version.split()[0])
        return 1
    chk("Python 版本", lambda: sys.version.split()[0])
    chk("模块 python-docx", lambda: need("docx"))
    chk("模块 openpyxl", lambda: need("openpyxl"))
    chk("模块 pypdf", lambda: need("pypdf"))
    chk("程序文件", files)
    chk("前端资源", vendor_test)
    chk("页面引用", html_refs)
    chk("可见窗口兜底（浏览器）", render_test)
    chk("使用指南", guide_test)
    chk("中文摘要", summary_test)
    chk("英文摘要", en_summary_test)
    chk("翻译分段对齐", translate_test)
    chk("Word 生成", docx_test)
    chk("汇总表生成", xlsx_test)
    if "--no-net" not in sys.argv:
        chk("外网抓取", fetch_test)

    print("-" * 58)
    for n, m in OK:
        print("  正常  %-16s %s" % (n, m))
    for n, m in BAD:
        print("  失败  %-16s %s" % (n, m))
    print("-" * 58)
    if BAD:
        print("有问题，先看上面的失败项。依赖缺就运行：")
        print("  python -m pip install -r requirements.txt")
        return 1
    print("全部正常，启动方式：")
    print('  双击 "%s"' % os.path.join(HERE, "启动.bat"))
    print("  或在命令行运行  python launch.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

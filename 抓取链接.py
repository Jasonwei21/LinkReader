# -*- coding: utf-8 -*-
"""
================================================================================
 批量抓取网页正文（单文件脚本，Spyder 可直接打开运行）
================================================================================

【先装库（只需一次）】
    在命令行 / Anaconda Prompt 里执行：

        pip install requests beautifulsoup4

【怎么用】
    1. 用 Spyder 打开本文件；
    2. 把链接填到下面「配置」里的 LINKS 列表（一行一个）；
    3. 按 F5 运行，进度和结果看下方控制台。

【链接放哪里】
    - 直接写在 LINKS = [...] 里，像注释里的例子那样一行一个；
    - 或者把链接一行一个放进同目录的 links.txt（# 开头是注释），
      LINKS 留空时脚本会自动读 links.txt。

【结果导出到哪里】
    默认导出到「本脚本同目录 / 导出」文件夹：
        001_文章标题.txt    每条正文一个文件
        报告.csv            记录每条的成功/失败、字数、原因
    想换位置，改下面的 OUT_DIR 即可。

【说明】
    - 只抓公开可读的正文；付费墙、验证码、反爬拦截页会被跳过，
      原因会写进 报告.csv；
    - 反爬很严的站点（Cloudflare / Akamai 等）可能返回 403，
      报告里会标出来，换时间重试或手动保存即可。
================================================================================
"""
import csv
import os
import time

# ---- 依赖：没装也不报红，运行时给一句明确提示 ----
try:
    import requests
    from bs4 import BeautifulSoup
    HAS_DEPS = True
except ImportError:
    requests = None
    BeautifulSoup = None
    HAS_DEPS = False


# =============================== 配  置 ===============================
# 1) 链接放这里：一行一个
LINKS = [
    # "https://www.example.com/article-1",
    # "https://www.example.com/article-2",
]

# 2) 链接文件（LINKS 为空时读它）
LINKS_FILE = "links.txt"

# 3) 结果导出到哪个文件夹（相对本脚本所在目录）
OUT_DIR = "导出"

# 4) 单条超时（秒）
TIMEOUT = 20
# =====================================================================


# 脚本所在目录。Spyder 按 F5 时 __file__ 可用；
# 若把代码粘到控制台跑（没有 __file__），退回当前工作目录。
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}


def real_path(p):
    """相对路径按脚本目录算，绝对路径原样返回。"""
    return p if os.path.isabs(p) else os.path.join(HERE, p)


# ------------------------------------------------------------------ 读链接
def read_links():
    """优先用 LINKS，否则读 links.txt；都没有就建一个空模板。"""
    links = [u.strip() for u in LINKS if u and u.strip()]
    if links:
        return links

    path = real_path(LINKS_FILE)
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 一行一个链接，# 开头的行是注释\n")
            f.write("# https://www.example.com/article-1\n")
        print("没找到 %s，已创建空模板，请填好链接后再运行。" % path)
        return []

    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


# ------------------------------------------------------------ 提取正文
def extract(html):
    """从 HTML 里取标题和正文段落。"""
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""

    # 去掉脚本、样式、导航、页脚等噪音
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "form", "noscript", "iframe", "svg"]):
        tag.decompose()

    # 优先从 <article> / <main> 里取，取不到就退回整个 body
    root = soup.find("article") or soup.find("main") or soup.body or soup

    blocks, seen = [], set()
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if len(text) >= 20 and text not in seen:   # 太短的（按钮、版权行）不要
            seen.add(text)
            blocks.append(text)

    return title, blocks


# ------------------------------------------------------------ 抓一条
def grab(url):
    """抓一条，返回 dict：ok / status / title / blocks / chars / reason。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:
        return {"ok": False, "status": "失败", "reason": str(e)[:120]}

    if resp.status_code != 200:
        return {"ok": False, "status": "HTTP %s" % resp.status_code,
                "reason": "服务器返回 %s，可能被反爬拦截" % resp.status_code}

    # requests 有时猜不准编码，用 apparent_encoding 兜一下，避免乱码
    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"

    title, blocks = extract(resp.text)
    chars = sum(len(b) for b in blocks)
    if chars < 200:
        return {"ok": False, "status": "无正文", "title": title,
                "reason": "只取到 %d 字，可能是拦截页或栏目页" % chars}

    return {"ok": True, "status": "成功", "title": title,
            "blocks": blocks, "chars": chars}


# ------------------------------------------------------------ 输出
def safe_name(i, title):
    """文件名：001_标题.txt，去掉 Windows 不允许的字符。"""
    title = (title or "").strip() or "无标题"
    for c in '\\/:*?"<>|\r\n\t':
        title = title.replace(c, "_")
    return "%03d_%s" % (i, title[:60])


def to_text(title, url, blocks):
    head = [
        title or "（无标题）",
        "原文：%s" % url,
        "字数：%d" % sum(len(b) for b in blocks),
        "抓取时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
    ]
    return "\n\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"


# ------------------------------------------------------------ 主流程
def main():
    if not HAS_DEPS:
        print("缺少依赖，请先在命令行执行：")
        print("    pip install requests beautifulsoup4")
        print("装完再运行本脚本。")
        return

    urls = read_links()
    if not urls:
        print("没有链接可抓。请把链接填到 LINKS 或 links.txt 里再运行。")
        return

    out_dir = real_path(OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 62)
    print("共 %d 条链接" % len(urls))
    print("导出目录：%s" % out_dir)
    print("=" * 62)

    rows = []
    ok_n = 0
    for i, url in enumerate(urls, 1):
        print("\n[%d/%d] %s" % (i, len(urls), url))
        t0 = time.time()
        rec = grab(url)

        filename = ""
        if rec["ok"]:
            filename = safe_name(i, rec.get("title"))
            with open(os.path.join(out_dir, filename + ".txt"), "w", encoding="utf-8") as f:
                f.write(to_text(rec.get("title"), url, rec["blocks"]))
            ok_n += 1
            print("    成功：%d 字，用时 %.1f 秒 -> %s.txt"
                  % (rec["chars"], time.time() - t0, filename))
        else:
            print("    跳过：%s（%s）" % (rec["reason"], rec["status"]))

        rows.append({
            "序号": i,
            "链接": url,
            "状态": rec["status"],
            "标题": rec.get("title") or "",
            "字数": rec.get("chars") or 0,
            "原因": rec.get("reason") or "",
            "文件": (filename + ".txt") if filename else "",
        })

    report = os.path.join(out_dir, "报告.csv")
    with open(report, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print("\n" + "=" * 62)
    print("完成：成功 %d 条，跳过 %d 条" % (ok_n, len(urls) - ok_n))
    print("正文目录：%s" % out_dir)
    print("报告文件：%s" % report)
    print("=" * 62)


if __name__ == "__main__":
    main()

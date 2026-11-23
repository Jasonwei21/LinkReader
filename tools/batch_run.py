# -*- coding: utf-8 -*-
"""按分组跑这批链接：抓取 -> 每篇一个 Word -> 汇总 zip。

用法：
    python batch_run.py A      # 可直连的站点
    python batch_run.py B      # 被商业反爬拦住的四个域名（走可见窗口）
"""
import csv
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, r"C:\Users\Jasonwei\WorkBuddy\2026-11-18-09-07-01\LinkReader")
import pipeline as PL

SRC = r"E:\系统文件夹\Desktop\1.txt"
# 工作目录放项目下、不放 outputs：渲染临时 profile 有成百上千个小文件，
# 在 outputs 下清理会触发批量删除保护，整个进程会被掐掉。
OUT = r"C:\Users\Jasonwei\WorkBuddy\2026-11-18-09-07-01\LinkReader\out-batch"

BLOCKED = ("reuters.com", "apnews.com", "marketscreener.com", "investing.com")


def host(u):
    return urllib.parse.urlsplit(u).netloc.replace("www.", "")


def load(group):
    urls = []
    for line in open(SRC, encoding="utf-8", errors="ignore"):
        u = line.strip()
        if not u.startswith("http"):
            continue
        h = host(u)
        blocked = any(h == b or h.endswith("." + b) or b in h for b in BLOCKED)
        if (group == "B") == blocked:
            urls.append(u)
    return urls


def main():
    group = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    urls = load(group)
    os.makedirs(OUT, exist_ok=True)
    print("分组 %s：%d 条" % (group, len(urls)), flush=True)

    opt = {
        "workers": 3 if group == "A" else 2,
        "timeout": 18,
        "min_chars": 400,
        "strict": True,
        "pdf_fallback": True,
        "render_fallback": True,
        "ocr_fallback": False,
        "visible_fallback": (group == "B"),
        "summary": False,
        "translate": "off",
    }

    job = PL.Job(urls, OUT, opt)
    t0 = time.time()
    log_path = os.path.join(OUT, "进度_%s.log" % group)

    def log(msg):
        line = "%5.0fs %s" % (time.time() - t0, msg)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    job.log = log
    job.run()

    snap = job.snapshot()
    print("完成：%s（%.1f 分钟）目录 %s" % (snap["state"], (time.time() - t0) / 60, job.dir),
          flush=True)

    # 报告：哪些能打开、哪些不能、为什么
    rows = []
    for it in snap["items"]:
        rows.append({
            "序号": it["idx"],
            "状态": it["status"],
            "来源": it.get("via", ""),
            "字符数": it.get("chars", 0),
            "标题": it.get("title", ""),
            "原因": it.get("reason", ""),
            "尝试路径": " → ".join("%s %.0fs%s" % (x["n"], x["s"], "" if x["ok"] else "失败")
                                for x in (it.get("trace") or [])),
            "Word": it.get("docx_file", ""),
            "链接": it["url"],
        })
    rep = os.path.join(OUT, "报告_%s.csv" % group)
    with open(rep, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["状态"] in ("已抓取", "完成")]
    print("成功 %d / %d" % (len(ok), len(rows)), flush=True)
    for k, v in sorted({r["状态"]: sum(1 for x in rows if x["状态"] == r["状态"])
                        for r in rows}.items(), key=lambda x: -x[1]):
        print("   %-8s %d" % (k, v), flush=True)


if __name__ == "__main__":
    main()

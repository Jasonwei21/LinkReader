# -*- coding: utf-8 -*-
"""批量抓取：读一个链接清单文件，整批跑完，输出报告 CSV 与进度 log。

用法：
    python tools/run_batch.py <链接清单.txt> [out_root] [--workers N] [--timeout N]

清单文件每行一个 URL。out_root 默认 out-batch/（缓存复用其中的 _cache，
上次抓成功过的页面这次直接从缓存读，不重复请求）。

输出（out_root/<时间戳>/）：
    txt/ md/         正文与 Markdown
    job.json         每条的状态、尝试路径
    报告.csv          序号/状态/来源/字符数/标题/原因/尝试路径/链接
    进度.log          运行日志
"""
import csv
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pipeline as PL  # noqa: E402


def main():
    argv = sys.argv[1:]
    # 位置参数：清单、out_root；开关：--workers N --timeout N
    # 注意不能用"过滤掉 -- 开头"的写法：--workers 的值 3 不带前缀，
    # 上个版本因此把 out_root 解析成了 "3"，输出全跑到了 LinkReader/3/ 下面。
    pos = []
    i = 0
    workers, timeout = 3, 40
    while i < len(argv):
        a = argv[i]
        if a == "--workers":
            workers = int(argv[i + 1])
            i += 2
        elif a == "--timeout":
            timeout = int(argv[i + 1])
            i += 2
        else:
            pos.append(a)
            i += 1
    if not pos:
        sys.exit("用法：python tools/run_batch.py <链接清单.txt> [out_root] [--workers N] [--timeout N]")
    urls_file = pos[0]
    out_root = pos[1] if len(pos) > 1 else os.path.join(ROOT, "out-batch")

    urls = [u.strip() for u in io.open(urls_file, encoding="utf-8") if u.strip()]
    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    print("清单 %d 条，去重后 %d 条" % (len(urls), len(ordered)), flush=True)

    opt = {
        "workers": workers, "timeout": timeout, "min_chars": 400,
        "strict": True, "pdf_fallback": True,
        "visible_fallback": True,
        "summary": False, "translate": "off",
    }

    job = PL.Job(ordered, out_root, opt)
    log_path = os.path.join(job.dir, "进度.log")
    os.makedirs(job.dir, exist_ok=True)      # Job 的目录要到 run() 才建，日志先开就得先建目录
    t0 = time.time()

    # 单一持久句柄 + 锁。原来的"每次追加都重新 open"在多线程下会撞
    # Windows 共享冲突（PermissionError），整个任务直接报异常收场。
    import threading
    _log_lock = threading.Lock()
    _log_f = io.open(log_path, "a", encoding="utf-8")

    def log(msg):
        line = "%6.0fs   %s" % (time.time() - t0, msg)
        print(line, flush=True)
        with _log_lock:
            try:
                _log_f.write(line + "\n")
                _log_f.flush()
            except Exception:
                pass                # 日志写不进去不能连累抓取

    job.log = log
    job.run()
    try:
        _log_f.close()
    except Exception:
        pass

    snap = job.snapshot()
    with io.open(os.path.join(job.dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump({"urls": ordered, "scraped_at": time.strftime("%Y-%m-%d %H:%M"),
                   "items": snap["items"]}, f, ensure_ascii=False, indent=1)

    with io.open(os.path.join(job.dir, "报告.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "状态", "来源", "字符数", "标题", "原因", "尝试路径", "链接"])
        for it in snap["items"]:
            trace = " → ".join(
                "%s %.0fs%s" % (x["n"], x["s"], "失败" if not x["ok"] else "")
                for x in (it.get("trace") or []))
            w.writerow([it["idx"], it.get("status"), it.get("via"),
                        it.get("chars"), it.get("title"),
                        it.get("reason"), trace, it["url"]])

    log("完成：%s" % snap["msg"])
    log("输出目录：%s" % job.dir)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""链接抓取摘要器 —— 本地网页版。

启动后浏览器自动打开，粘链接 → 抓正文 → 生成摘要 → 下载 txt / md / Word / 汇总表。
只用 Python 标准库起服务，不装 web 框架也能跑。
"""
import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pipeline as PL  # noqa: E402
import sheet_links as SL  # noqa: E402

# 打成 exe 之后，代码和 static 被解到临时目录（sys._MEIPASS），是只读的；
# out/ 和 config.json 要留在 exe 旁边，用户才找得到自己的结果。
FROZEN = bool(getattr(sys, "frozen", False))
RES = getattr(sys, "_MEIPASS", HERE) if FROZEN else HERE
DATA = os.path.dirname(sys.executable) if FROZEN else HERE

STATIC = os.path.join(RES, "static")
OUT_ROOT = os.path.join(DATA, "out")
CONFIG = os.path.join(DATA, "config.json")

# 心跳：exe 模式下页面每 5 秒报一次到，关掉浏览器就把它收掉。
# 普通 python 启动时 enabled 为 False，这条链路完全不参与。
BEAT = {"enabled": False, "last": time.time(), "bye": 0.0, "seen": False, "start": time.time()}

BEAT_JS = r"""
(function () {
  if (!window.fetch) return;
  function beat() {
    try { fetch('/api/ping', { cache: 'no-store' }); } catch (e) {}
  }
  beat();
  setInterval(beat, 5000);
  /* 后台标签页的定时器会被浏览器限流到 1 分钟一次，切回来先补一拍 */
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) beat();
  });
  /* 关页面时立刻通知，不必等到下一次心跳 */
  window.addEventListener('pagehide', function () {
    try { navigator.sendBeacon('/api/bye'); } catch (e) {}
  });
})();
"""

DEFAULT_CFG = {
    "llm": {"enabled": False, "base_url": "https://api.openai.com/v1",
            "api_key": "", "model": "gpt-4o-mini"},
    "opt": {"workers": 3, "timeout": 40, "min_chars": 400,
            "pdf_fallback": True, "strict": True,
            "summary": True, "translate": "off"},
}
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}$")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json",
        ".md": "text/markdown; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class PastJob(object):
    """已经跑完、进程里已经没有的任务：从 out/<id>/job.json 还原。

    对外接口和 pipeline.Job 保持一致（snapshot / dir / zpath），
    这样 /api/job/<id> 的读取、下载、打包三套逻辑可以原样复用。
    """

    def __init__(self, jid):
        self.id = jid
        self.dir = os.path.join(OUT_ROOT, jid)
        zp = os.path.join(self.dir, "结果打包.zip")
        self.zpath = zp if os.path.isfile(zp) else None
        self._snap = None

    def snapshot(self):
        if self._snap is None:
            self._snap = self._build()
        return self._snap

    def _build(self):
        with open(os.path.join(self.dir, "job.json"), encoding="utf-8") as f:
            data = json.load(f)
        items, done = [], 0
        for i, rec in enumerate(data.get("items") or [], 1):
            ok = bool(rec.get("ok"))
            if ok:
                done += 1
            rel, rel_docx = "", ""
            if ok:
                name = PL.base_name(rec, i)
                if os.path.isfile(os.path.join(self.dir, "txt", name + ".txt")):
                    rel = "txt/%s.txt" % name
                if os.path.isfile(os.path.join(self.dir, "docx", name + ".docx")):
                    rel_docx = "docx/%s.docx" % name
            items.append({
                "idx": i, "url": rec.get("url", ""),
                "status": "完成" if ok else (rec.get("status") or "未收录"),
                "title": rec.get("title") or "",
                "chars": rec.get("chars") or 0,
                "summary": rec.get("summary") or "",
                "reason": rec.get("reason") or "",
                "file": rel, "docx_file": rel_docx,
                "translated": bool(rec.get("trans_blocks")),
                "keywords": rec.get("keywords") or [],
                "summary_how": rec.get("summary_how") or "",
                "site": rec.get("site") or "", "date": rec.get("date") or "",
                "translate": rec.get("translate") or "off",
            })
        return {"id": self.id,
                "state": "done" if done else "empty",
                "msg": "历史任务 · %s" % (data.get("scraped_at") or self.id),
                "done": done, "total": len(items), "elapsed": 0,
                "items": items,
                "zip": os.path.basename(self.zpath) if self.zpath else ""}


def past_job(jid):
    """id 合法且目录里真有 job.json 才认，挡住路径穿越。"""
    if not JOB_ID_RE.match(jid or ""):
        return None
    if not os.path.isfile(os.path.join(OUT_ROOT, jid, "job.json")):
        return None
    return PastJob(jid)


def history_list(limit=40):
    """扫 out/ 下跑过的任务，新的在前。只看 job.json，不读正文，很快。"""
    out = []
    if not os.path.isdir(OUT_ROOT):
        return out
    for name in sorted(os.listdir(OUT_ROOT), reverse=True):
        if not JOB_ID_RE.match(name):
            continue
        d = os.path.join(OUT_ROOT, name)
        jf = os.path.join(d, "job.json")
        if not os.path.isfile(jf):
            continue
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        items = data.get("items") or []
        first = ""
        for x in items:
            if x.get("title"):
                first = x["title"]
                break
        out.append({
            "id": name,
            "time": data.get("scraped_at") or "",
            "total": len(items),
            "done": sum(1 for x in items if x.get("ok")),
            "first": first or (items[0].get("url") if items else ""),
            "has_zip": os.path.isfile(os.path.join(d, "结果打包.zip")),
        })
        if len(out) >= limit:
            break
    return out



def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                user = json.load(f)
            for k in ("llm", "opt"):
                if isinstance(user.get(k), dict):
                    cfg[k].update(user[k])
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def mask(cfg):
    c = json.loads(json.dumps(cfg))
    k = c.get("llm", {}).get("api_key") or ""
    if k:
        c["llm"]["api_key_masked"] = k[:4] + "…" + k[-4:] if len(k) > 10 else "已设置"
    c["llm"]["api_key"] = ""
    c["llm"]["has_key"] = bool(k)
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = "LinkReader/1.0.0"

    def log_message(self, fmt, *a):
        pass  # 静音访问日志，终端只留关键提示

    # ---------------------------------------------------------- 基础
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 8 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _raw_body(self, limit=16 * 1024 * 1024):
        """拖进来的表格是原始字节，不走 JSON。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n <= 0 or n > limit:
            return b""
        try:
            return self.rfile.read(n)
        except (ConnectionResetError, OSError):
            # 用户中途取消了拖放，连接就断了，不值得打一屏幕栈
            return b""

    def _file(self, path, download_name=None):
        if not os.path.isfile(path):
            self._json({"error": "文件不存在"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        if download_name:
            ctype = "application/octet-stream"
        extra = {}
        if download_name:
            from urllib.parse import quote
            extra["Content-Disposition"] = (
                "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (download_name.encode("ascii", "ignore").decode() or "file",
                   quote(download_name)))
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype, extra)

    def _safe(self, base, rel):
        p = os.path.realpath(os.path.join(base, rel))
        return p if p.startswith(os.path.realpath(base) + os.sep) else None

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = unquote(u.path)

        if p in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if p.startswith("/static/"):
            f = self._safe(STATIC, p[len("/static/"):])
            return self._file(f) if f else self._json({"error": "非法路径"}, 400)

        if p == "/api/config":
            return self._json(mask(load_cfg()))

        if p == "/api/ping":
            BEAT["last"] = time.time()
            BEAT["seen"] = True
            BEAT["bye"] = 0.0        # 刷新页面时旧页面发过 bye，这里撤销
            return self._json({"ok": True})

        # 心跳客户端只在这个开关打开时给（exe 启动器才会打开），
        # 用 python app.py 跑的时候返回空注释，页面不会去发心跳。
        if p == "/api/beat.js":
            js = BEAT_JS if BEAT["enabled"] else "/* 未启用心跳 */"
            return self._send(200, js.encode("utf-8"),
                              "application/javascript; charset=utf-8")

        if p == "/api/history":
            return self._json({"jobs": history_list()})

        if p.startswith("/api/job/"):
            parts = p.strip("/").split("/")
            jid = parts[2] if len(parts) > 2 else ""
            job = JOBS.get(jid) or past_job(jid)
            if not job:
                return self._json({"error": "任务不存在或已过期"}, 404)
            if len(parts) == 3:
                return self._json(job.snapshot())
            if len(parts) == 4 and parts[3] == "zip":
                if not job.zpath or not os.path.isfile(job.zpath):
                    return self._json({"error": "打包文件还没生成"}, 404)
                return self._file(job.zpath, "抓取结果_%s.zip" % jid)
            if len(parts) == 4 and parts[3] == "file":
                rel = (q.get("path") or [""])[0]
                f = self._safe(job.dir, rel)
                if not f:
                    return self._json({"error": "非法路径"}, 400)
                return self._file(f, os.path.basename(f))

        return self._json({"error": "未找到"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p == "/api/bye":
            # 标签页要关了，给了个“准备收工”的信号；宽限期里有新 ping 就撤销
            BEAT["bye"] = time.time()
            return self._json({"ok": True})

        # 表格走原始字节，必须放在 _body() 之前：
        # _body() 会按 Content-Length 把 body 读空，之后再读就永远阻塞了
        if p == "/api/parse-file":
            name = (q.get("name") or [""])[0]
            blob = self._raw_body()
            if not blob:
                return self._json({"error": "没收到文件内容（也可能超过 16MB）"}, 400)
            try:
                return self._json(SL.parse(name, blob))
            except SL.SheetError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": "读表格时出错：%s" % e}, 500)

        data = self._body()

        if p == "/api/extract":
            urls = PL.extract_urls(data.get("text") or "")
            return self._json({"urls": urls, "count": len(urls)})

        if p == "/api/config":
            cfg = load_cfg()
            llm = data.get("llm") or {}
            for k in ("enabled", "base_url", "model"):
                if k in llm:
                    cfg["llm"][k] = llm[k]
            if llm.get("api_key"):
                cfg["llm"]["api_key"] = llm["api_key"].strip()
            if llm.get("clear_key"):
                cfg["llm"]["api_key"] = ""
            opt = data.get("opt") or {}
            for k in ("workers", "timeout", "min_chars"):
                if k in opt:
                    try:
                        cfg["opt"][k] = int(opt[k])
                    except (TypeError, ValueError):
                        pass
            for k in ("pdf_fallback", "strict", "summary"):
                if k in opt:
                    cfg["opt"][k] = bool(opt[k])
            if opt.get("translate") in ("off", "zh", "both"):
                cfg["opt"]["translate"] = opt["translate"]
            save_cfg(cfg)
            return self._json({"ok": True, "config": mask(cfg)})

        if p == "/api/test_llm":
            import summarizer as SZ
            cfg = load_cfg()
            llm = dict(cfg["llm"])
            if (data.get("api_key") or "").strip():
                llm["api_key"] = data["api_key"].strip()
            for k in ("base_url", "model"):
                if data.get(k):
                    llm[k] = data[k]
            try:
                s, k_, model = SZ.llm_summary(
                    [["p", "这是一段用于测试接口连通性的样例文字。文章讨论了中国电动车出口"
                           "在欧洲市场面临的关税调整，以及欧盟碳边境机制对产业链的影响。"
                           "作者认为短期内价格优势仍然存在，但本地化生产会成为常态。"]],
                    "接口测试", llm, timeout=60)
                return self._json({"ok": True, "sample": s[:200],
                                   "keywords": k_, "model": model})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if p == "/api/job":
            urls = [PL.norm_url(u) for u in (data.get("urls") or [])
                    if isinstance(u, str) and u.startswith("http")]
            urls = list(dict.fromkeys(urls))
            if not urls:
                return self._json({"error": "没有识别到有效链接"}, 400)
            if len(urls) > 60:
                return self._json({"error": "一次最多 60 条，建议分批"}, 400)
            cfg = load_cfg()
            opt = dict(cfg["opt"])
            for k in ("workers", "timeout", "min_chars", "pdf_fallback", "strict",
                      "summary"):
                if k in (data.get("opt") or {}):
                    opt[k] = data["opt"][k]
            jopt = data.get("opt") or {}
            mode = jopt.get("translate")
            if mode in ("off", "zh", "both"):
                opt["translate"] = mode
            opt["llm"] = dict(cfg["llm"])
            if (data.get("llm") or {}).get("enabled") is not None:
                opt["llm"]["enabled"] = bool(data["llm"]["enabled"])

            # 翻译只能靠大模型，没配接口就直接在前端拦住，别等跑完一堆任务才报错
            import llm as LLM
            if opt.get("translate") in ("zh", "both") and not LLM.configured(opt["llm"]):
                return self._json({"error": "翻译需要先配置大模型接口（设置里填 base_url / "
                                            "API Key / 模型名，或先关掉翻译）"}, 400)
            job = PL.Job(urls, OUT_ROOT, opt)
            with JOBS_LOCK:
                JOBS[job.id] = job
                for old in list(JOBS)[:-20]:
                    JOBS.pop(old, None)
            threading.Thread(target=job.run, daemon=True).start()
            return self._json({"id": job.id, "total": len(urls)})

        return self._json({"error": "未找到"}, 404)


def free_port(start=8765):
    for p in range(start, start + 40):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def make_server(port):
    """建一个已绑定端口的服务对象，exe 启动器要自己拿去做生命周期管理。"""
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser(description="链接抓取摘要器")
    ap.add_argument("-p", "--port", type=int, default=0, help="端口，默认自动挑一个空端口")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args()

    os.makedirs(OUT_ROOT, exist_ok=True)
    port = a.port or free_port()
    srv = make_server(port)
    url = "http://127.0.0.1:%d/" % port
    print("=" * 58)
    print("  链接抓取摘要器已启动")
    print("  地址：%s" % url)
    print("  停止：在这个窗口按 Ctrl+C")
    print("=" * 58)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

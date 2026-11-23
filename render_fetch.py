# -*- coding: utf-8 -*-
"""浏览器渲染兜底：用本机已装的 Edge / Chrome 无头渲染页面，取渲染后的 HTML。

为什么需要它
    urllib 只拿到服务端吐的原始 HTML。有些站点的正文是 JS 插进去的，
    原始 HTML 里只有 <div id="app"></div>；还有的站用 Cloudflare 之类做挑战，
    裸请求直接 403 或超时。这时候"用浏览器打开"就能看见——本模块就是干这个的。

边界
    只做渲染，不做任何"过验证"的动作：不点验证码、不伪造指纹、不覆盖指纹参数。
    订阅墙/付费墙由调用方（pipeline）先行判定，命中就直接跳过，不走这里。

工程要点
    1. exe 是 --noconsole 打包的，子进程必须带 CREATE_NO_WINDOW，否则闪黑窗。
    2. headless 进程超时后要杀整棵进程树，否则 msedge 会留一堆僵尸进程。
    3. 每次用独立 --user-data-dir，避免和用户正在用的浏览器抢 profile 锁。
"""
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time

CREATE_NO_WINDOW = 0x08000000

# 常见安装位置，按命中概率排序
_CANDIDATES = [
    (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", "Edge"),
    (r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe", "Edge"),
    (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", "Chrome"),
    (r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", "Chrome"),
    (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", "Chrome"),
]

# 注册表 App Paths：装在非标准目录时靠这个找
_APP_PATHS = [
    ("msedge.exe", "Edge"),
    ("chrome.exe", "Chrome"),
]

_browser_cache = []


def _from_registry(exe_name):
    try:
        import winreg
    except ImportError:
        return ""
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe_name) as k:
                path = winreg.QueryValue(k, None)
                if path and os.path.isfile(path):
                    return path.strip('"')
        except OSError:
            continue
    return ""


def find_browser():
    """返回 (exe 路径, 浏览器名)；找不到就是 ("", "")。结果缓存。"""
    if _browser_cache:
        return _browser_cache[0]
    result = ("", "")
    for raw, name in _CANDIDATES:
        path = os.path.expandvars(raw)
        if os.path.isfile(path):
            result = (path, name)
            break
    if not result[0]:
        for exe_name, name in _APP_PATHS:
            path = _from_registry(exe_name)
            if path:
                result = (path, name)
                break
    _browser_cache.append(result)
    return result


def available():
    return bool(find_browser()[0])


_NET_TEXT = {
    "ERR_CONNECTION_CLOSED": "连接被中断",
    "ERR_CONNECTION_RESET": "连接被重置",
    "ERR_CONNECTION_REFUSED": "连接被拒绝",
    "ERR_TIMED_OUT": "加载超时",
    "ERR_CONNECTION_TIMED_OUT": "连接超时",
    "ERR_ADDRESS_UNREACHABLE": "地址不可达",
    "ERR_NETWORK_CHANGED": "网络切换中",
    "ERR_HTTP2_PROTOCOL_ERROR": "HTTP/2 协议错误",
    "ERR_NAME_NOT_RESOLVED": "域名解析失败",
    "ERR_TUNNEL_CONNECTION_FAILED": "代理隧道握手失败",
    "ERR_PROXY_CONNECTION_FAILED": "代理连不上",
    "ERR_EMPTY_RESPONSE": "服务器返回空响应",
    "ERR_CERT_AUTHORITY_INVALID": "证书不受信任",
    "ERR_SSL_PROTOCOL_ERROR": "SSL 握手失败",
    "ERR_INTERNET_DISCONNECTED": "本机没联网",
}

# 这些是瞬时故障，同一条链接再开一次浏览器往往就过了
# 这些是"快速失败"，同一条链接再开一次浏览器往往就过了。
# 刻意不含各种 TIMED_OUT：一次已经二十多秒，网络真不通时再等一次纯属浪费。
_NET_TRANSIENT = ("ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET",
                  "ERR_EMPTY_RESPONSE",
                  "ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED")


def _net_error(err_bytes):
    """从 stderr 里挑出 Chromium 的网络错误码，返回 (code, 中文说明)。"""
    try:
        text = err_bytes.decode("utf-8", "ignore")
    except Exception:
        return "", ""
    found = re.findall(r"net::(ERR_[A-Z0-9_]+)", text)
    if not found:
        return "", ""
    code = found[-1]
    return code, _NET_TEXT.get(code, code)


def _kill_tree(proc):
    """先 taskkill /T 杀整棵树，再不济 proc.kill()。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _run_browser(args, timeout):
    """跑一次 headless，返回 (returncode, stdout bytes, stderr bytes, timed_out)。"""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        return -1, b"", str(e).encode("utf-8", "ignore"), False
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
        return -9, out, err, True


def _base_args(exe, profile_dir, width=1280, height=1200, budget_ms=5000):
    return [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--hide-scrollbars",
        "--user-data-dir=" + profile_dir,
        "--window-size=%d,%d" % (width, height),
        "--virtual-time-budget=%d" % budget_ms,
    ]


_TLS = threading.local()


def _profile_for(work_dir):
    """当前线程在这个 work_dir 下的固定渲染 profile。

    Edge 的 user-data-dir 一旦建好就有几百个文件；原来每次渲染
    mkdtemp 一个新的、用完 rmtree 删掉——整批跑下来又是几千个文件的
    创建加删除，又慢又容易在半路卡住。现在按线程复用：Edge 退出后
    profile 仍然有效，下次渲染直接用（预热过的 profile 起步还快些）。
    程序内不做批量删除，profile 目录留在 _render/ 下，想清理时手动删。
    """
    base = work_dir or tempfile.gettempdir()
    profs = getattr(_TLS, "profs", None)
    if profs is None:
        profs = _TLS.profs = {}
    prof = profs.get(base)
    if prof is None or not os.path.isdir(prof):
        os.makedirs(base, exist_ok=True)
        prof = tempfile.mkdtemp(prefix="lr-render-", dir=base)
        profs[base] = prof
    return prof


def dump_dom(url, timeout=45, budget_ms=5000, width=1280, height=1200, work_dir=None, log=None):
    """渲染后取整页 HTML。返回 (html | None, note)。"""
    exe, name = find_browser()
    if not exe:
        return None, "本机没找到 Edge / Chrome，无法渲染"

    for attempt in (0, 1):
        profile = _profile_for(work_dir)
        try:
            args = _base_args(exe, profile, width, height, budget_ms) + ["--dump-dom", url]
            t0 = time.time()
            rc, out, err, timed = _run_browser(args, timeout)
            took = time.time() - t0
            if timed:
                # 超时不再重试：一次已经等满 timeout，再等一次只是翻倍，
                # 真要救回来那是用户重跑的事。
                return None, "浏览器渲染超时（%d 秒）" % timeout
            html = out.decode("utf-8", "ignore")
            low = html.lower()
            if "<html" in low or "<body" in low:
                if log:
                    log("    [渲染] %s 取到 %d 字节" % (name, len(html)))
                return html, ""

            code, text = _net_error(err)
            # 只对"很快就断"的错误重试一次：几秒内断连往往是抖动，
            # 等了几十秒才断说明这条路就是不通，再来一遍纯属浪费。
            if code in _NET_TRANSIENT and attempt == 0 and took < 10:
                if log:
                    log("    [渲染] %s，重试一次" % text)
                continue
            if code:
                return None, "页面没打开：%s（%s）" % (text, code)
            msg = err.decode("utf-8", "ignore").strip().splitlines()
            if not msg:
                # rc=0 且 stdout / stderr 都空：浏览器正常退出但一个字节没吐。
                # 常见于连接在握手阶段就断了（代理中途掐掉），Chromium 来不及
                # 生成错误页。这不是页面内容的问题，也不是能重试解决的。
                return None, "浏览器没打开页面（无输出，多为网络或代理中断）"
            return None, "%s 渲染无结果（%s）" % (name, msg[-1][:80])
        finally:
            pass   # profile 按线程复用，不再逐次删除（见 _profile_for）
    return None, "浏览器渲染失败"


def screenshot(url, png_path, timeout=45, budget_ms=5000, width=1280, height=1200,
               work_dir=None, log=None):
    """渲染并把首屏截成 PNG。返回 (成功?, note)。"""
    exe, name = find_browser()
    if not exe:
        return False, "本机没找到 Edge / Chrome，无法截图"

    profile = _profile_for(work_dir)
    args = _base_args(exe, profile, width, height, budget_ms) + ["--screenshot=" + png_path, url]
    rc, out, err, timed = _run_browser(args, timeout)
    if timed:
        return False, "截图超时（%d 秒）" % timeout
    if not os.path.isfile(png_path) or os.path.getsize(png_path) < 2000:
        code, text = _net_error(err)
        if code:
            return False, "截图失败：%s（%s）" % (text, code)
        return False, "%s 截图失败（rc=%s）" % (name, rc)
    if log:
        log("    [截图] %d 字节" % os.path.getsize(png_path))
    return True, ""


def shot(url, cache_dir, timeout=45, budget_ms=5000, width=1280, height=1200, log=None):
    """按需截图（不走 render）。返回 (png 路径 | "", note)。

    单独拆出来是因为截图要 15 秒上下：DOM 已经拿到正文时就没必要截。
    """
    work_dir = None
    if cache_dir:
        work_dir = os.path.join(cache_dir, "_render")
        os.makedirs(work_dir, exist_ok=True)
    tag = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    path = os.path.join(work_dir or tempfile.gettempdir(), "shot-%s.png" % tag)
    ok, note = screenshot(url, path, timeout=timeout, budget_ms=budget_ms,
                          width=width, height=height, work_dir=work_dir, log=log)
    return (path if ok else ""), note


def render(url, cache_dir, want_shot=False, timeout=45, budget_ms=5000,
           width=1280, height=1200, log=None):
    """渲染一次，返回 dict：
        raw   渲染后的 HTML（失败为 None）
        shot  截图路径（want_shot 且成功时才有）
        how   "render" / "shot" / ""
        note  失败原因或空
    """
    work_dir = None
    if cache_dir:
        work_dir = os.path.join(cache_dir, "_render")
        os.makedirs(work_dir, exist_ok=True)

    res = {"raw": None, "shot": "", "how": "", "note": ""}
    html, note = dump_dom(url, timeout=timeout, budget_ms=budget_ms,
                          width=width, height=height, work_dir=work_dir, log=log)
    if html:
        res["raw"] = html
        res["how"] = "render"

    if want_shot:
        # 文件名带 url 指纹：多线程抓取时不会互相覆盖
        tag = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        shot = os.path.join(work_dir or tempfile.gettempdir(), "shot-%s.png" % tag)
        ok, snote = screenshot(url, shot, timeout=timeout, budget_ms=budget_ms,
                               width=width, height=height, work_dir=work_dir, log=log)
        if ok:
            res["shot"] = shot
            if not res["how"]:
                res["how"] = "shot"
        elif not res["note"]:
            res["note"] = snote

    if not res["raw"] and not res["note"]:
        res["note"] = note or "渲染失败"
    return res

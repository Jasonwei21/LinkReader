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
import shutil
import subprocess
import tempfile

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


def dump_dom(url, timeout=45, budget_ms=5000, width=1280, height=1200, work_dir=None, log=None):
    """渲染后取整页 HTML。返回 (html | None, note)。"""
    exe, name = find_browser()
    if not exe:
        return None, "本机没找到 Edge / Chrome，无法渲染"

    profile = tempfile.mkdtemp(prefix="lr-render-", dir=work_dir)
    try:
        args = _base_args(exe, profile, width, height, budget_ms) + ["--dump-dom", url]
        rc, out, err, timed = _run_browser(args, timeout)
        if timed:
            return None, "浏览器渲染超时（%d 秒）" % timeout
        html = out.decode("utf-8", "ignore")
        low = html.lower()
        if "<html" not in low and "<body" not in low:
            msg = err.decode("utf-8", "ignore").strip().splitlines()
            tail = msg[-1][:80] if msg else ("无输出，rc=%s" % rc)
            return None, "%s 渲染无结果（%s）" % (name, tail)
        if log:
            log("    [渲染] %s 取到 %d 字节" % (name, len(html)))
        return html, ""
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def screenshot(url, png_path, timeout=45, budget_ms=5000, width=1280, height=1200,
               work_dir=None, log=None):
    """渲染并把首屏截成 PNG。返回 (成功?, note)。"""
    exe, name = find_browser()
    if not exe:
        return False, "本机没找到 Edge / Chrome，无法截图"

    profile = tempfile.mkdtemp(prefix="lr-shot-", dir=work_dir)
    try:
        args = _base_args(exe, profile, width, height, budget_ms) + ["--screenshot=" + png_path, url]
        rc, out, err, timed = _run_browser(args, timeout)
        if timed:
            return False, "截图超时（%d 秒）" % timeout
        if not os.path.isfile(png_path) or os.path.getsize(png_path) < 2000:
            return False, "%s 截图失败（rc=%s）" % (name, rc)
        if log:
            log("    [截图] %d 字节" % os.path.getsize(png_path))
        return True, ""
    finally:
        shutil.rmtree(profile, ignore_errors=True)


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

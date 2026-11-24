# -*- coding: utf-8 -*-
"""可见窗口兜底：用真实浏览器窗口打开页面，等它渲染完，再取已渲染的 DOM。

为什么需要这一级
    有的站点（Reuters 的 DataDome、AP 的 Cloudflare）会识别无头浏览器，
    直接返回一个校验页。无头模式怎么调都拿不到正文，但**同一个地址用平时
    那个浏览器打开是能看的**。这里就照这个思路做：开一个真窗口，等页面
    自己出来，再把屏幕上那份 DOM 取走。

边界
    - 只取"已经渲染出来的东西"，不注入脚本去骗过校验、不碰验证码。
    - 需要人点验证码的站点，这里到点就放弃，如实报"需要人工处理"。
    - 窗口是可见的，用户能看到它在做什么。

实现
    Edge/Chrome 以 --remote-debugging-port 启动，用 CDP（DevTools 协议）
    控制导航与取值。CDP 走 WebSocket，这里用标准库实现了一个够用的客户端，
    不引入任何第三方依赖，exe 体积不变。

    Runtime.evaluate 取 document.documentElement.outerHTML 拿到的就是
    渲染后的整页 HTML，交给 core_fetch 的正文抽取即可。
"""
import base64
import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import render_fetch as RF

CREATE_NO_WINDOW = 0x08000000

# 窗口关闭提示的统一标记：pipeline 靠它稳定识别"窗口没了"，
# 不再依赖某句精确文案（崩溃/异常退出走别的分支时文案不同，早先会漏判）。
CLOSED_MARK = "窗口已关闭"


def closed_note():
    """窗口不在了时给上层的一句提示，带可识别标记。"""
    return (CLOSED_MARK + "：浏览器窗口不在了（手动关闭 / 程序结束 / 异常退出），"
            "本条未抓完；可点「再试一次」重开窗口补抓")


_STATE = {
    "proc": None,           # 当前这条抓取的浏览器进程（每条一个新窗口）
    "port": 0,
    "profile": "",
    "exe": "",
    "name": "",
    "app_closing": False,   # 程序主动关窗（抓完/停止/任务结束），区别于用户手关
    "lock": threading.Lock(),
}

# 可见抓取一次只做一条：一条一个窗口、抓完整篇关掉再开下一条。
# 并发既会被机器人校验限流（实测 3 并只有 1 条过），也让用户看不清过程。
_VIS_LOCK = threading.Semaphore(1)


class _NamedMutex(object):
    """Windows 命名互斥量：把「开窗口抓取」在多个 LinkReader 实例之间也串起来。

    _VIS_LOCK 只在进程内有效。两个实例同时跑时，各自都会开窗口，桌面上
    就是"同时跳好几下"；更糟的是两边共用同一个浏览器 profile，Edge 的
    单实例机制会让一个窗口被另一个实例接管，本实例就误判成"窗口关闭"。
    这个互斥量保证所有实例加起来同一时刻只有一个抓取窗口。
    """

    def __init__(self, name):
        self.name = name
        self.handle = None

    def acquire(self, timeout_ms):
        if os.name != "nt":
            return True
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.CreateMutexW(None, False, self.name)
            if not h:
                return True                     # 拿不到锁就退回进程内串行
            r = k32.WaitForSingleObject(h, int(timeout_ms))
            if r in (0x00000000, 0x00000080):   # WAIT_OBJECT_0 / WAIT_ABANDONED
                self.handle = h
                return True
            k32.CloseHandle(h)
            return False
        except Exception:
            return True

    def release(self):
        if not self.handle:
            return
        try:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None


_GLOBAL_VIS = _NamedMutex("Local\\LinkReader_VisibleFetch_v1")


# ---------------------------------------------------------------- WebSocket
class _WS(object):
    """够用的 WebSocket 客户端：只处理 CDP 需要的文本收发。"""

    def __init__(self, ws_url, timeout=30):
        if not ws_url.startswith("ws://"):
            raise IOError("不是 ws 地址：%s" % ws_url[:40])
        rest = ws_url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.timeout = timeout
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (path, hostport, key)
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("握手时连接断开")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise IOError("握手被拒绝")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send_text(self, text):
        data = text.encode("utf-8")
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + masked)

    def _exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise IOError("连接已断开")
            out += chunk
        return out

    def recv_text(self):
        while True:
            b1, b2 = self._exact(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mk = self._exact(4) if masked else None
            data = self._exact(n) if n else b""
            if mk:
                data = bytes(c ^ mk[i % 4] for i, c in enumerate(data))
            if opcode == 8:
                raise IOError("对端关闭连接")
            if opcode == 9:                      # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                if not (b1 & 0x80):              # 分片：继续读到结束帧
                    while True:
                        h1, h2 = self._exact(2)
                        fin = h1 & 0x80
                        nn = h2 & 0x7F
                        if nn == 126:
                            nn = struct.unpack(">H", self._exact(2))[0]
                        elif nn == 127:
                            nn = struct.unpack(">Q", self._exact(8))[0]
                        data += self._exact(nn) if nn else b""
                        if fin:
                            break
                return data.decode("utf-8", "ignore")


def _cdp(ws, mid, method, params=None, timeout=30):
    ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
    t0 = time.time()
    while True:
        left = timeout - (time.time() - t0)
        if left <= 0:
            raise IOError("CDP 超时：%s" % method)
        # socket 超时收紧到本次剩余时限：死页签上 recv 会一直阻塞，
        # 只靠这里的时间判断掐不断它（实测一轮能挂满 30 秒）。
        try:
            ws.sock.settimeout(max(0.5, min(ws.timeout, left)))
            msg = json.loads(ws.recv_text())
        except socket.timeout:
            raise IOError("CDP 超时：%s" % method)
        if msg.get("id") == mid:
            return msg


def _eval(ws, mid, expr, timeout=20):
    r = _cdp(ws, mid, "Runtime.evaluate",
             {"expression": expr, "returnByValue": True, "awaitPromise": True},
             timeout=timeout)
    return (r.get("result") or {}).get("result", {}).get("value")


# ---------------------------------------------------------------- 浏览器进程
def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _http_json(path, port, timeout=5):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _launch_browser(cache_dir, log=None):
    """为「一条」抓取启动一个全新的可见窗口。

    一条一个窗口：启动 → 抓到完整正文 → 关窗口 → 再开下一条。
    整批由 _VIS_LOCK 串行，用户桌面上任一时刻只有一个抓取窗口，
    能看清「这条在抓什么、抓完关掉、再开下一条」的节奏。

    上一条是强杀收尾的，profile 里可能残留 Singleton 锁；不清理的话
    新实例会以为已有实例在跑，直接退出（rc=0，调试端口起不来）。
    """
    with _STATE["lock"]:
        # 兜底：万一上一条还残留（异常路径没关干净），先收掉再开。
        stale = _STATE.get("proc")
        if stale is not None and stale.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(stale.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=CREATE_NO_WINDOW, timeout=10)
            except Exception:
                pass
        exe, name = RF.find_browser()
        if not exe:
            raise IOError("本机没找到 Edge / Chrome")
        # 每个进程用独立 profile：万一还是同时跑起两个实例，Edge 也不会
        # 因为它们共用一个 user-data-dir 而互相接管窗口。
        prof = os.path.join(cache_dir, "_visible_profile_%d" % os.getpid())
        os.makedirs(prof, exist_ok=True)
        for fn in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(prof, fn))
            except OSError:
                pass
        port = _free_port()
        args = [exe,
                "--remote-debugging-port=%d" % port,
                "--user-data-dir=" + prof,
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-sync",
                "--mute-audio",
                "--window-size=1100,820", "--window-position=140,90",
                "about:blank"]
        if log:
            log("    [可见] 新开窗口 %s" % name)
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 25:
            try:
                _http_json("/json/version", port)
                _STATE.update({"proc": proc, "port": port, "profile": prof,
                               "exe": exe, "name": name, "app_closing": False})
                return port
            except Exception:
                if proc.poll() is not None:
                    raise IOError("浏览器启动后立刻退出（rc=%s）" % proc.returncode)
                time.sleep(0.4)
        try:
            proc.kill()
        except Exception:
            pass
        raise IOError("浏览器调试端口没起来")


def browser_dead():
    """可见浏览器进程是否已经不在了。

    返回 (gone, by_app)：gone=窗口没了；by_app=是程序主动关的（停止/任务结束）。
    用户中途把窗口关掉（by_app=False），等价于说"这条别抓了"：
    调用方据此立即停止等待、把该条记为「窗口关闭」，允许「再试一次」救回。
    程序主动关窗（by_app=True）是正常收尾，不该误报成"用户关的"。
    """
    with _STATE["lock"]:
        p = _STATE.get("proc")
        gone = p is None or p.poll() is not None
        return gone, bool(_STATE.get("app_closing"))


def close_browser():
    """任务结束时收掉窗口，别在用户桌面上留一堆进程。"""
    with _STATE["lock"]:
        proc = _STATE.get("proc")
        _STATE["proc"] = None
        _STATE["app_closing"] = True   # 标记：这次消失是程序主动关的
        if proc is None:
            return
        try:
            _http_json("/json/close/", _STATE.get("port") or 0, timeout=2)
        except Exception:
            pass
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=10)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- 取页面
def _new_tab(port, url="about:blank"):
    """开一个标签页，返回 (target_id, ws_url)。

    一律先开 about:blank，再由调用方用 Page.navigate 显式导航。
    早期版本把目标 url 塞进 /json/new?<url> 让 Chrome 自己导航，
    随后又用 Page.navigate 导航同一个地址，两次导航互相竞争，
    偶发把页面卡在 about:blank（用户看到的就是「空白窗口」）。
    """
    q = urllib.parse.quote(url, safe="")
    req = urllib.request.Request("http://127.0.0.1:%d/json/new?%s" % (port, q),
                                 method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        t = json.loads(r.read().decode("utf-8", "ignore"))
    return t.get("id"), t.get("webSocketDebuggerUrl")


def _work_tab(port):
    """拿到本窗口里唯一那个工作标签，返回 (target_id, ws_url)。

    每条抓取都是一个新开的窗口，以 about:blank 启动、天然带一个 page 标签。
    就用这一个标签、只用 Page.navigate 切到目标地址——所以窗口里始终只有
    一个标签，不会再出现"第二个标签是 about:blank"（早先每条 _new_tab
    新建，新标签先停在 about:blank，navigate 一失败就露出空白页）。

    只有标签被关光（一个 page 都没有）才新建一个兜底。
    """
    try:
        pages = _http_json("/json/list", port)
        for p in pages:
            if p.get("type") == "page" and p.get("webSocketDebuggerUrl"):
                return p.get("id"), p.get("webSocketDebuggerUrl")
    except Exception:
        pass
    return _new_tab(port, "about:blank")


def _close_tab(port, tid):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/close/%s" % (port, tid),
                               timeout=5).read()
    except Exception:
        pass


def fetch(url, cache_dir, min_text=800, max_wait=45, settle=1.5, log=None):
    """在一个全新的可见窗口里打开 url，抓完就关，返回 (html, note)。

    一条一个窗口、串行执行：实测并发开 3 个标签机器人校验只放行 1 条，
    而且并发会让用户看不清每条在抓什么。这里排队，每条独占一个窗口，
    抓到完整正文（或判定失败）后由 finally 关掉，下一条再新开。
    """
    # 先跨进程排队（最多等 3 分钟），再进进程内串行。这样即便不小心开了
    # 两个 LinkReader，桌面上也永远只有一个抓取窗口，不会同时跳好几个。
    if not _GLOBAL_VIS.acquire(180000):
        return None, "另一个 LinkReader 实例正在抓取，本条先跳过（等它空闲后再点「再试一次」）"
    try:
        with _VIS_LOCK:
            return _fetch(url, cache_dir, min_text=min_text, max_wait=max_wait,
                          settle=settle, log=log)
    finally:
        _GLOBAL_VIS.release()


def _fetch(url, cache_dir, min_text=800, max_wait=45, settle=1.5, log=None):
    try:
        port = _launch_browser(cache_dir, log=log)
    except Exception as e:
        return None, "可见窗口起不来：%s" % e

    ws = None
    try:
        # 复用浏览器里那一个标签，不再每条新建——否则浏览器里会出现
        # 第二个标签、且它先停在 about:blank，正是用户看到的"第二个空白页"。
        tid, ws_url = _work_tab(port)
        if not ws_url:
            return None, "开标签页失败"
        ws = _WS(ws_url, timeout=30)
        _cdp(ws, 1, "Page.enable")
        _cdp(ws, 2, "Runtime.enable")
        # 把工作标签提到前台，用户看到的就是正在抓取的页面。
        try:
            _cdp(ws, 3, "Page.bringToFront", timeout=6)
        except Exception:
            pass

        def navigate(mid):
            r = _cdp(ws, mid, "Page.navigate", {"url": url}, timeout=30)
            return (r.get("result") or {}).get("errorText") or ""

        # 导航返回一个 errorText 不代表真失败（ERR_ABORTED 是重定向/重复
        # 导航的正常现象；页面也可能正在加载）。早先一有 errorText 就
        # return，标签随即被 finally 关掉——用户看到"窗口 1 秒就关了"。
        # 现在记录错误但继续轮询，交给下面的正文判定决定成败。
        nav_err = navigate(4)
        nav_retries = 0

        t0 = time.time()
        best, best_len = None, 0
        while time.time() - t0 < max_wait:
            time.sleep(settle)
            # 窗口没了：用户手关 / 程序收尾 / 崩溃，都立刻停止并如实上报。
            # 统一带 CLOSED_MARK 标记，pipeline 靠它稳定识别成「窗口关闭」。
            gone, by_app = browser_dead()
            if gone:
                return None, closed_note() if not by_app else \
                    CLOSED_MARK + "：任务已停止或结束，窗口已收起"
            # 15 秒了页面上还是一个字都没有：地址多半打不开
            # （连接超时、被代理掐断），别把 max_wait 等满。
            if time.time() - t0 > 15 and best_len == 0:
                return None, "窗口打开 15 秒页面无内容（多半打不开）"
            try:
                # 轮询求值必须短超时：死页签上 CDP 会一直挂到超时，
                # 用默认 20 秒的话，一轮循环就是 30 多秒，15 秒判据形同虚设。
                href = _eval(ws, 14, "location.href", timeout=6) or ""
                state = _eval(ws, 10, "document.readyState", timeout=6)
                n = _eval(ws, 11, "(document.body?document.body.innerText.length:0)", timeout=6) or 0
                title = _eval(ws, 12, "document.title", timeout=6) or ""
            except Exception:
                continue
            # 导航竞态补救：过了几秒还停在 about:blank，说明导航没落地，
            # 主动再导航一次（最多两次），别让用户在空白页干等到超时。
            if href in ("about:blank", "") and time.time() - t0 > 3:
                if nav_retries < 2:
                    nav_retries += 1
                    navigate(40 + nav_retries)
                    continue
            # 连接打不开时窗口里是 Edge 的错误页——有文字，所以上面的
            # "无内容"判据抓不住它。文本很短时探一眼正文，认出错误页
            # （"无法访问此页面" / ERR_xxx / can't reach）就立刻放弃。
            if 0 < n < 600:
                probe = _eval(ws, 13, "document.body ? document.body.innerText.slice(0,400) : ''",
                              timeout=6) or ""
                pl = probe.lower()
                if ("ERR_" in probe or "无法访问此页面" in probe
                        or "此网页无法" in probe or "can't reach this page" in pl
                        or "hmmm…can't reach" in pl
                        or "access denied" in pl or "forbidden" in pl
                        or "机器人验证" in probe):
                    return None, "窗口里是拦截/错误页（%s）" % (title or "ERR")[:60]
            if n > best_len:
                best_len = n
                best = (state, n, title)
            # 正文够长就认为页面真的出来了。interactive 也收：DOM 已经
            # 建好、正文在屏幕上了，剩下的是图片广告之类的次资源，
            # 等它们没意义——BBC 实测要白等 30 秒才 complete。
            if state in ("complete", "interactive") and n >= min_text:
                break
        if log:
            log("    [可见] %.0fs readyState=%s 文本 %d 字「%s」" % (
                time.time() - t0, (best or ("?", 0, ""))[0], best_len,
                ((best or ("", 0, ""))[2] or "")[:40]))

        final_href = ""
        try:
            final_href = _eval(ws, 21, "location.href", timeout=6) or ""
        except Exception:
            pass
        html = _eval(ws, 20, "document.documentElement.outerHTML", timeout=30)
        if not html:
            return None, "没取到页面内容（可能还在校验中）" + \
                ("（导航提示：%s）" % nav_err[:40] if nav_err else "")
        if final_href in ("about:blank", "") and best_len == 0:
            return None, "窗口停留在 about:blank，导航未落地" + \
                ("（导航提示：%s）" % nav_err[:40] if nav_err else "")
        if best_len < min_text:
            return html, "页面文本仅 %d 字，可能仍在机器人校验" % best_len
        return html, ""
    except Exception as e:
        return None, "可见窗口取页面失败：%s" % str(e)[:80]
    finally:
        if ws is not None:
            ws.close()
        # 一条抓完（拿到完整正文 / 判定失败 / 被用户关掉）就关掉这个窗口，
        # 下一条 fetch 会 _launch_browser 新开一个。用户看到的就是
        # 「开这条的窗口 → 抓完关掉 → 再开下一条」一条一条来。
        # 关窗统一走 close_browser()，它会置 app_closing=True，
        # pipeline 据此不会把程序正常关窗误判成「用户手动关闭」。
        close_browser()

# -*- coding: utf-8 -*-
"""LinkReader 桌面启动器（打 exe 用，双击不弹黑窗口）。

三件事：
  1. 起一个本地服务，自动用默认浏览器打开；
  2. 页面每 5 秒发一次心跳；关掉页面会先收到一次"拜拜"，6 秒宽限期后收掉服务，
     没收到"拜拜"（比如直接关浏览器进程）就靠 90 秒无心跳兜底；
  3. 出错不静默——写日志 + 弹一个原生提示框，用户不用对着"闪一下"猜。

单实例：已经有一个在跑的时候，第二次双击只把浏览器指向老端口，不会再起一个。
"""
import ctypes
import os
import sys
import threading
import time
import traceback
import webbrowser

import app as APP

IDLE_LIMIT = 90.0     # 秒。后台标签页的定时器会被浏览器限流到 1 分钟一次，留足余量
BYE_GRACE = 6.0       # 关页面后的宽限期，防"刷新页面"被误判成关闭
BOOT_GRACE = 120.0    # 浏览器迟迟没连上的容忍时间


def here():
    """exe 所在目录 / 源码目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def state_dir():
    """日志和端口记录放这里，不污染程序目录（Program Files 下本来也写不进去）。"""
    d = os.environ.get("LOCALAPPDATA") or here()
    d = os.path.join(d, "LinkReader")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = here()
    return d


LOG_PATH = os.path.join(state_dir(), "launcher.log")
PORT_PATH = os.path.join(state_dir(), "port.txt")

# 单实例锁：Windows 命名互斥量在进程存活期间一直持有。
# 早先只靠 port.txt 判断，两个实例几乎同时双击时会双双读空、双双启动，
# 于是两个服务各开各的可见窗口、还共用同一个浏览器 profile 互相抢窗口。
_MUTEX = {"handle": None}


def acquire_single_instance():
    """原子地争抢单实例锁：True=本实例拿到，False=已有实例在跑。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "Local\\LinkReader_SingleInstance_v1")
        if not h:
            return True                      # 系统不给锁就退回旧逻辑
        r = k32.WaitForSingleObject(h, 0)
        if r in (0x00000000, 0x00000080):    # WAIT_OBJECT_0 / WAIT_ABANDONED
            _MUTEX["handle"] = h             # 持有到进程退出，由系统自动释放
            return True
        k32.CloseHandle(h)
        return False
    except Exception:
        return True


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def alert(title, text):
    """没有控制台，只能用系统弹窗把话说清楚。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def alive(port):
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def read_port():
    try:
        with open(PORT_PATH, encoding="utf-8") as f:
            p = int(f.read().strip())
        return p if alive(p) else 0
    except Exception:
        return 0


def watch(srv, port):
    """心跳看门狗：页面没了就收工。"""
    beat = APP.BEAT
    beat["start"] = time.time()
    beat["last"] = time.time()
    log("服务已起：http://127.0.0.1:%d/" % port)

    reason = ""
    while True:
        time.sleep(1.0)
        now = time.time()

        # bye 先判：哪怕一次心跳都没收到（页面刚开就被关掉），也一样能收工
        if beat["bye"] and (now - beat["bye"] > BYE_GRACE):
            reason = "标签页已关闭"
            break

        if not beat["seen"]:
            if now - beat["start"] > BOOT_GRACE:
                reason = "浏览器一直没连上来"
                break
            continue

        if now - beat["last"] > IDLE_LIMIT:
            reason = "心跳超时 %.0f 秒" % IDLE_LIMIT
            break

    log("收工：%s" % reason)
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass


def open_browser(url):
    # 调试/自动化测试时不想弹窗口，设 LINKREADER_NO_BROWSER=1
    if os.environ.get("LINKREADER_NO_BROWSER") == "1":
        log("跳过自动开浏览器（LINKREADER_NO_BROWSER=1）")
        return
    webbrowser.open(url)


def main():
    APP.BEAT["enabled"] = True

    old = read_port()
    if old:
        log("已有实例在 %d，直接开浏览器" % old)
        open_browser("http://127.0.0.1:%d/" % old)
        return 0

    # 互斥量真正保证「只起一个服务」：同时双击也只有一个能往下走。
    if not acquire_single_instance():
        # 另一个实例正在启动，等它把 port.txt 写出来再指过去（最多 10 秒）
        for _ in range(50):
            old = read_port()
            if old:
                log("已有实例在 %d（互斥量判重），直接开浏览器" % old)
                open_browser("http://127.0.0.1:%d/" % old)
                return 0
            time.sleep(0.2)
        log("检测到已有实例，但一时读不到端口，本次不再起服务")
        return 0

    os.makedirs(APP.OUT_ROOT, exist_ok=True)
    port = APP.free_port()
    srv = APP.make_server(port)
    url = "http://127.0.0.1:%d/" % port

    try:
        with open(PORT_PATH, "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Timer(0.5, lambda: open_browser(url)).start()
    log("启动 %s" % url)

    try:
        watch(srv, port)
    finally:
        try:
            os.remove(PORT_PATH)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    # 没有控制台时 sys.stdout 是 None，任何 print 都会炸；这里兜一下
    if sys.stdout is None:
        try:
            sys.stdout = open(LOG_PATH, "a", encoding="utf-8")
        except Exception:
            pass
    if sys.stderr is None:
        sys.stderr = sys.stdout
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        log("崩了：\n" + traceback.format_exc())
        alert("LinkReader 启动失败",
              "启动时出错，详情记在：\n%s\n\n%s"
              % (LOG_PATH, traceback.format_exc().strip().splitlines()[-1]))
        sys.exit(1)

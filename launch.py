# -*- coding: utf-8 -*-
"""启动器：查环境 → 补依赖 → 起服务。

单独抽出来的原因：批处理文件（.bat）在中文 Windows 上按 GBK 解析，
里面写中文提示会把命令字节吃掉，导致 "pip install" 被拆成 "'e'"、
"'penpyxl'" 这种诡异报错。所以 .bat 只保留 ASCII，中文全部放这里。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (import 名, pip 包名)
PKGS = [
    ("docx", "python-docx"),
    ("openpyxl", "openpyxl"),
    ("pypdf", "pypdf"),
]

# 依次尝试：默认源 → 清华 → 阿里。内网或代理环境第一个常失败。
MIRRORS = [
    (None, None),
    ("https://pypi.tuna.tsinghua.edu.cn/simple", "清华镜像"),
    ("https://mirrors.aliyun.com/pypi/simple", "阿里云镜像"),
]


def setup_console():
    """Windows 控制台默认 GBK，输出中文可能抛 UnicodeEncodeError。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def say(msg=""):
    print(msg, flush=True)


def rule(ch="─", n=58):
    say(ch * n)


def missing():
    out = []
    for mod, pkg in PKGS:
        try:
            __import__(mod)
        except ImportError:
            out.append(pkg)
    return out


def pip_install(pkgs, index=None, user=False):
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--no-input"]
    if user:
        cmd.append("--user")
    if index:
        cmd += ["-i", index]
    cmd += pkgs
    say("  $ " + " ".join(cmd[1:]))
    try:
        return subprocess.call(cmd) == 0
    except Exception as e:
        say("  启动 pip 失败：%s" % e)
        return False


def ensure_deps():
    lack = missing()
    if not lack:
        say("依赖检查：齐全（python-docx / openpyxl / pypdf）")
        return True

    say("依赖检查：缺少 " + "、".join(lack))
    say("")
    for index, label in MIRRORS:
        for user in (False, True):
            tag = label or "PyPI 默认源"
            say("正在安装（%s%s）…" % (tag, "，用户目录" if user else ""))
            if pip_install(lack, index, user):
                if not missing():
                    say("安装成功。")
                    say("")
                    return True
                say("pip 返回成功但模块仍不可用，换下一个源。")
            say("这个源没装上，换下一个。")
            say("")
    return False


def main():
    setup_console()
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        os.system("")  # 打开 Windows 控制台的 ANSI 支持

    say("")
    rule("═")
    say("  链接抓取摘要器 · LinkReader")
    rule("═")
    say("Python %d.%d.%d" % sys.version_info[:3])
    say("")

    if sys.version_info < (3, 9):
        say("需要 Python 3.9 或更高版本，当前版本过低。")
        return 1

    if not ensure_deps():
        say("")
        rule()
        say("依赖安装失败。多半是网络或代理限制，手动装一次即可：")
        say("")
        say("  python -m pip install -r requirements.txt "
            "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        say("")
        say("如果提示权限不足，在后面加 --user：")
        say("")
        say("  python -m pip install --user -r requirements.txt")
        say("")
        rule()
        return 1

    sys.path.insert(0, HERE)
    os.chdir(HERE)
    import app as APP
    APP.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

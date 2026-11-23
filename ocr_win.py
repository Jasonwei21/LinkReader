# -*- coding: utf-8 -*-
"""系统 OCR 兜底：调用 Windows 自带的 Windows.Media.Ocr 识别截图里的文字。

为什么用系统 OCR 而不是打包 tesseract / PaddleOCR
    后两者会给 exe 加 40-300 MB，而且 tesseract 还得单独装语言包。
    Windows 10/11 自带 OCR 引擎，跟着系统语言包走，零体积、零安装。

实现方式
    走 PowerShell 调 WinRT。脚本内嵌在本文件里，运行时落到临时目录，
    以 -ExecutionPolicy Bypass 拉起（企业机器常把脚本执行策略设成 Restricted）。
    识别结果让 PowerShell 写成 UTF-8 JSON 文件再读回来——不走 stdout，
    因为控制台代码页会把中文变成乱码。

质量提醒
    这个通道是**有损**的：行会被断开、表格会散、连字符会残留、形近字会错。
    Windows 默认只带系统语言的识别包（中文系统通常只有 zh-Hans-CN 和 ja），
    没有 en 时识别英文会出现大小写错乱。所以调用方必须把结果标成"需校对"，
    不能跟 DOM 抓取的正文混为一谈。
"""
import json
import os
import re
import subprocess
import tempfile

CREATE_NO_WINDOW = 0x08000000

_PS_SCRIPT = r"""
param([string]$Image, [string]$Out, [string]$Lang = "")

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

function Write-Json($obj) {
    $json = $obj | ConvertTo-Json -Depth 5 -Compress
    [System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Image)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
if ($Lang -ne "") {
    try {
        $langObj = New-Object Windows.Globalization.Language($Lang)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langObj)
    } catch { $engine = $null }
}
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if (-not $engine -and $Lang -eq "") {
    foreach ($tag in @("en-US", "zh-Hans-CN", "ja")) {
        try {
            $langObj = New-Object Windows.Globalization.Language($tag)
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langObj)
            if ($engine) { break }
        } catch { }
    }
}
if (-not $engine) {
    Write-Json ([pscustomobject]@{ error = "no_engine"; lang = ""; lines = @() })
    exit 0
}

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = @()
foreach ($line in $result.Lines) {
    $minX = 0.0; $minY = 0.0; $maxX = 0.0; $maxY = 0.0; $first = $true
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        if ($first) {
            $minX = $r.X; $minY = $r.Y; $maxX = $r.X + $r.Width; $maxY = $r.Y + $r.Height; $first = $false
        } else {
            if ($r.X -lt $minX) { $minX = $r.X }
            if ($r.Y -lt $minY) { $minY = $r.Y }
            if (($r.X + $r.Width) -gt $maxX) { $maxX = $r.X + $r.Width }
            if (($r.Y + $r.Height) -gt $maxY) { $maxY = $r.Y + $r.Height }
        }
    }
    $lines += [pscustomobject]@{
        text = $line.Text
        x = [int][math]::Round($minX)
        y = [int][math]::Round($minY)
        w = [int][math]::Round($maxX - $minX)
        h = [int][math]::Round($maxY - $minY)
    }
}

Write-Json ([pscustomobject]@{
    error = ""
    lang = $engine.RecognizerLanguage.LanguageTag
    width = [int]$bitmap.PixelWidth
    height = [int]$bitmap.PixelHeight
    lines = $lines
})
"""

_script_path = []


def _ensure_script(work_dir=None):
    """把内嵌脚本落到磁盘，返回路径。只写一次。"""
    if _script_path:
        return _script_path[0]
    base = work_dir or os.path.join(tempfile.gettempdir(), "LinkReader")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "lr_ocr.ps1")
    # UTF-8 with BOM：PowerShell 5.1 读无 BOM 的 UTF-8 会按 ANSI 解，中文注释会乱
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(_PS_SCRIPT)
    _script_path.append(path)
    return path


def _powershell():
    for name in ("powershell.exe", "pwsh.exe"):
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "WindowsPowerShell", "v1.0", name)
        if os.path.isfile(exe):
            return exe
    return "powershell.exe"


def languages(timeout=25, work_dir=None):
    """返回本机 OCR 可用语言标签列表，例如 ['ja', 'zh-Hans-CN']。不可用返回 []。"""
    ps = ("$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime];"
          "$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime];"
          "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | "
          "ForEach-Object { $_.LanguageTag }")
    try:
        proc = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=timeout)
        out = proc.stdout.decode("utf-8", "ignore")
        return [x.strip() for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def ocr_image(image_path, lang="", timeout=90, work_dir=None):
    """识别一张图。返回 dict：
        error  空串表示成功
        lang   实际使用的识别语言
        width / height
        lines  [{text, x, y, w, h}]
    """
    res = {"error": "", "lang": "", "width": 0, "height": 0, "lines": []}
    if not image_path or not os.path.isfile(image_path):
        res["error"] = "图片不存在"
        return res

    # WinRT 的 GetFileFromPathAsync 只认反斜杠，必须规范化
    image_path = os.path.normpath(os.path.abspath(image_path))

    script = _ensure_script(work_dir)
    out_dir = os.path.dirname(script) or tempfile.gettempdir()
    out_json = os.path.normpath(os.path.join(out_dir, "lr_ocr_out.json"))
    if os.path.exists(out_json):
        try:
            os.remove(out_json)
        except OSError:
            pass

    try:
        proc = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Image", image_path, "-Out", out_json, "-Lang", lang or ""],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        res["error"] = "OCR 超时（%d 秒）" % timeout
        return res
    except Exception as e:
        res["error"] = "OCR 调用失败：%s" % str(e)[:60]
        return res

    if not os.path.isfile(out_json):
        err = proc.stderr.decode("utf-8", "ignore").strip().splitlines()
        res["error"] = "OCR 无输出（%s）" % (err[-1][:80] if err else "未知原因")
        return res

    try:
        with open(out_json, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        res["error"] = "OCR 结果解析失败：%s" % str(e)[:60]
        return res

    if data.get("error"):
        res["error"] = "本机没有可用的 OCR 识别包"
        return res

    res["lang"] = data.get("lang") or ""
    res["width"] = data.get("width") or 0
    res["height"] = data.get("height") or 0
    res["lines"] = data.get("lines") or []
    return res


# ---------------------------------------------------------------- 文本整理
_CJK = r"\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
# 汉字 + 全角标点：这两类字符之间不该有空格
_SPACEY = _CJK + r"，。！？；：、（）《》「」『』“”‘’…—·"
_SENT_END = re.compile(r"[。！？；：.!?;:。，,、»”\"'）)\]】]$")
_BULLET = re.compile(r"^[•·▸▪\-—*]\s*")
_ENDS_HYPHEN = re.compile(r"[A-Za-z]-$")


def _trim_cjk_spaces(s):
    """OCR 常把中文按字框拆开塞空格，这里去掉汉字/全角标点之间的空格。"""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"(?<=[" + _SPACEY + r"])[ \t]+(?=[" + _SPACEY + r"])", "", s)
    return s


def merge_lines(lines, gap_ratio=1.15):
    """把 OCR 的逐行结果合并成段落。

    规则：
      行距 > 行高 * gap_ratio          -> 断段
      上一行以句末标点结尾且行距偏大     -> 断段
      上一行以连字符结尾、下一行小写开头 -> 拼回去（去掉连字符）
      其他                              -> 同行续接（中文不加空格，西文加空格）
    """
    rows = [ln for ln in lines if (ln.get("text") or "").strip()]
    rows.sort(key=lambda d: (d.get("y", 0), d.get("x", 0)))

    paras = []
    buf = ""
    prev = None

    def flush():
        nonlocal buf
        t = buf.strip()
        if t:
            paras.append(_trim_cjk_spaces(t))
        buf = ""

    for ln in rows:
        text = (ln.get("text") or "").strip()
        h = max(ln.get("h") or 0, 8)
        gap = (ln.get("y", 0) - (prev.get("y", 0) + prev.get("h", 0))) if prev else 999

        if prev is not None:
            if gap > h * gap_ratio:
                flush()

        is_bullet = bool(_BULLET.match(text))
        if is_bullet and buf:
            flush()

        if not buf:
            buf = text
        elif _ENDS_HYPHEN.search(buf) and re.match(r"^[a-z]", text):
            buf = buf[:-1] + text
        elif re.search(r"[" + _CJK + r"]$", buf) or re.match(r"^[" + _CJK + r"]", text):
            buf = buf + text
        else:
            buf = buf + " " + text

        prev = ln

    flush()
    return paras


def blocks_from_ocr(result, title=""):
    """OCR 结果 -> 与 core_fetch 一致的 [(tag, text)] 结构。"""
    if not result or result.get("error"):
        return []
    paras = merge_lines(result.get("lines") or [])
    blocks = []
    if title:
        blocks.append(("h1", title))
    for p in paras:
        if len(p) < 2:
            continue
        # 短行且无句末标点，多半是小标题
        tag = "h2" if len(p) <= 40 and not _SENT_END.search(p) else "p"
        blocks.append((tag, p))
    return blocks


def quality_note(result, page_lang=""):
    """给结果打一个质量提示；latin 页面但只有中日文引擎时要明确警告。"""
    lang = (result or {}).get("lang") or ""
    if not lang:
        return "OCR 未启用"
    if page_lang == "en" and not lang.lower().startswith("en"):
        return "OCR 用 %s 识别英文，大小写与形近字易错，需逐段校对" % lang
    if lang.lower().startswith("zh") and page_lang == "en":
        return "OCR 用中文引擎识别英文，建议安装英文语言包后重跑"
    return "OCR 识别（%s），断行与标点需校对" % lang

# -*- coding: utf-8 -*-
"""全文翻译：按段对齐、分批送、结果长度必须跟原文段数一致。

为什么不用「整篇丢进去让它翻」：长文会超上下文、模型会自动合并段落，段落一合并
后面就没法跟原文对齐了。所以按段切成 JSON 数组送，要求原样返回同样长度的数组。
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import llm

MAX_CHARS = 30000          # 超过这个长度只翻前面部分，避免一次任务烧掉太多额度
CHUNK_CHARS = 2600         # 单次请求的原文长度上限
CHUNK_BLOCKS = 12          # 单次请求的段数上限
CJK = re.compile(r"[\u4e00-\u9fff]")

SYS = (
    "你是专业译者，把用户发来的 JSON 数组里每个字符串翻译成简体中文。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 字符串数组，不要任何解释、不要 ``` 代码块标记。\n"
    "2. 输出数组长度必须和输入完全一致，顺序一一对应，不能合并、拆分或省略。\n"
    "3. 已经是中文的条目原样返回，不要改写。\n"
    "4. 人名、机构名、项目名第一次出现用「中文（English）」格式，同一条目内后文只用中文。\n"
    "5. 数字、日期、百分比、金额照原样，不要换算或估算。\n"
    "6. 不添加原文没有的内容，不写译注，不总结。\n"
    "7. 保留原文的语气和句间逻辑连接词。\n"
)


def _is_zh(s):
    letters = len(CJK.findall(s))
    return letters >= 6 and letters / float(max(1, len(s))) > 0.4


def _chunks(items):
    """items: [(idx, text)] → 分批，控制总字符与段数。"""
    out, cur, size = [], [], 0
    for idx, text in items:
        n = len(text)
        if cur and (size + n > CHUNK_CHARS or len(cur) >= CHUNK_BLOCKS):
            out.append(cur)
            cur, size = [], 0
        cur.append((idx, text))
        size += n
    if cur:
        out.append(cur)
    return out


def _parse_array(raw, expect):
    s = llm.strip_fence(raw)
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        arr = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    arr = [x if isinstance(x, str) else ("" if x is None else str(x)) for x in arr]
    return arr if len(arr) == expect else None


def _translate_chunk(cfg, chunk):
    """返回 {idx: 译文}，失败抛异常。"""
    texts = [t for _, t in chunk]
    raw = llm.chat(cfg, [{"role": "system", "content": SYS},
                         {"role": "user", "content": json.dumps(texts, ensure_ascii=False)}],
                   temperature=0.1, timeout=180)
    arr = _parse_array(raw, len(texts))
    if arr is None:
        # 模型没按数组返回，退到逐段翻译，慢但不会错位
        arr = []
        for t in texts:
            r = llm.chat(cfg, [{"role": "system", "content": SYS},
                               {"role": "user", "content": json.dumps([t], ensure_ascii=False)}],
                         temperature=0.1, timeout=120)
            one = _parse_array(r, 1)
            arr.append(one[0] if one else llm.strip_fence(r))
    return {idx: (arr[k] or orig) for k, (idx, orig) in enumerate(chunk)}


def translate_title(title, cfg):
    if not title:
        return ""
    if _is_zh(title):
        return title
    try:
        raw = llm.chat(cfg, [{"role": "system", "content": SYS},
                             {"role": "user", "content": json.dumps([title], ensure_ascii=False)}],
                       temperature=0.1, timeout=60)
        one = _parse_array(raw, 1)
        return (one[0] if one else llm.strip_fence(raw)).strip() or title
    except Exception:
        return ""


def translate(blocks, cfg, title="", on_progress=None, workers=4):
    """返回 (trans_blocks, title_zh, note)。trans_blocks 与 blocks 等长。

    抛 llm.LLMError 时调用方负责降级。
    """
    if not llm.configured(cfg):
        raise llm.LLMError("大模型配置不完整（需要 base_url / api_key / model）")

    # 预算截断
    total, keep, cut = 0, 0, False
    for tag, text in blocks:
        if total + len(text) > MAX_CHARS:
            cut = True
            break
        total += len(text)
        keep += 1
    work = blocks[:keep]

    # 已经是中文的段不送，省 token
    todo = [(i, t) for i, (tag, t) in enumerate(work) if not _is_zh(t)]
    done_map = {}
    errs = []
    lock = threading.Lock()
    total_n = len(todo)
    finished = [0]

    def run(chunk):
        try:
            got = _translate_chunk(cfg, chunk)
        except Exception as e:
            # 单批失败不拖垮整篇：这一段留空，后面用原文补
            with lock:
                errs.append(str(e))
            got = {}
        with lock:
            done_map.update(got)
            finished[0] += len(chunk)
            if on_progress:
                try:
                    on_progress(finished[0], total_n)
                except Exception:
                    pass

    if todo:
        batches = _chunks(todo)
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as ex:
            list(ex.map(run, batches))

    out = []
    for i, (tag, text) in enumerate(blocks):
        if i >= keep:
            out.append((tag, text))          # 超出预算的保持原文
        elif i >= len(work):
            out.append((tag, text))
        else:
            zh = done_map.get(i)
            out.append((tag, zh.strip() if zh and zh.strip() else text))

    n_missing = 0
    for i, _ in todo:
        if not done_map.get(i):
            n_missing += 1

    # 一整篇都没译出来，说明接口不通或配置错，别伪装成「已完成」
    if todo and n_missing >= len(todo):
        raise llm.LLMError(errs[0] if errs else "全部段落翻译失败")

    note = "大模型（%s）" % cfg.get("model", "")
    if n_missing:
        note += "，%d 段未翻译保留原文" % n_missing
    if cut:
        note += "，超出 %d 字符的部分未翻译" % MAX_CHARS
    title_zh = translate_title(title, cfg) if title else ""
    return out, title_zh, note

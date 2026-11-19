# -*- coding: utf-8 -*-
"""摘要：默认本地提取式（离线、不要 key），配置了大模型则改用大模型。

本地提取式的思路是经典的「句子打分 + 冗余抑制」：
  1. 按句切分，统计词频与逆句频（idf），给每句算权重
  2. 标题里出现过的词加权，首段和末段加权
  3. 贪心挑选，跟已选句子重合度太高的跳过，避免五句话讲同一件事
  4. 按原文顺序输出，摘要读起来才连贯

它只能挑原句、不会改写，所以英文原文出来还是英文句子；要大模型那种中文改写
摘要，就在设置里填一个 OpenAI 兼容接口。
"""
import json
import math
import re

import llm

# ---------------------------------------------------------------- 停用词
STOP_EN = set("""
a about above after again against all also am an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does doesn't
doing don't down during each few for from further had hadn't has hasn't have haven't having
he her here hers herself him himself his how however i if in into is isn't it its itself
just let's me more most mustn't my myself no nor not of off on once only or other ought our
ours ourselves out over own same shan't she should shouldn't so some such than that the
their theirs them themselves then there these they this those through to too under until up
very was wasn't we were weren't what when where which while who whom why will with won't
would wouldn't you your yours yourself yourselves said says say one two three new now may
might must also would us them thus therefore while since though although much many well
way ways thing things part parts make makes made take takes taken come comes came go goes
went get gets got give gives given see sees seen know knows known think thinks thought
according per within across among around back even still yet ever never
""".split())

STOP_ZH = set("""
的 了 和 是 在 就 都 而 及 与 着 或 一个 没有 我们 你们 他们 她们 它们 这个 那个 这些 那些
自己 什么 怎么 为什么 因为 所以 但是 然而 而且 并且 如果 虽然 可以 可能 应该 已经 还是 还有
对于 关于 通过 进行 表示 认为 指出 目前 现在 同时 此外 以及 其中 这种 这种 方面 情况 问题
不会 不能 不是 就是 也是 但是 只是 这样 那样 一些 一样 需要 必须 能够 由于 因此 从而
人士 专家 业内 相关 有关 工作 不断 持续 仍然 依然 目前 已经 方面 情况 问题 表示 认为
""".split())

# 提取式摘要最爱抓的噪音词：出现频率高但没有信息量
JUNK = set("""
http https www com org net html htm php pdf doi figure fig table source sources note notes
author authors available accessed retrieved copyright reserved rights university press
journal vol pp et al ibid email twitter linkedin facebook share subscribe newsletter
read more click here download view print comment comments article articles paper papers
report reports study studies data year years month day week time number percent cent
""".split())

SENT_EN = re.compile(r"(?<=[.!?])[\"”’)]?\s+(?=[\"“'A-Z0-9])")
SENT_ZH = re.compile(r"(?<=[。！？；!?;])")
WORD_EN = re.compile(r"[a-zA-Z][a-zA-Z'\-]{1,}")
CJK = re.compile(r"[\u4e00-\u9fff]")

# 参考文献、脚注、页脚这类段落不是正文，摘要里出现会明显拉低质量
REF_HEAD = re.compile(r"^\s*(references?|bibliography|works cited|endnotes?|footnotes?|"
                      r"sources?|参考文献|参考资料|注释|引用文献)\s*[:：]?\s*$", re.I)
CITE = re.compile(r"(available at\s+https?|accessed\s+(on|at)\b|\bdoi[:. ]|\[\d{1,3}\]|"
                  r"\bet al\.?\s*\(|^\S{2,20}\s*[（(]\d{4}[a-z]?[）)]|"
                  r"\bpp?\.\s*\d|retrieved from|https?://)", re.I)


def _is_cite(s):
    return bool(CITE.search(s))


def _sent_split(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = []
    for chunk in SENT_ZH.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 中文句号切完，再按英文句点切一次（中英混排常见）
        for sub in SENT_EN.split(chunk):
            sub = sub.strip()
            if sub:
                parts.append(sub)
    return parts


def _tokens(s):
    low = s.lower()
    toks = [w for w in WORD_EN.findall(low)
            if w not in STOP_EN and w not in JUNK and len(w) > 2]
    cjk = "".join(CJK.findall(s))
    for i in range(len(cjk) - 1):
        bg = cjk[i:i + 2]
        if bg not in STOP_ZH:
            toks.append(bg)
    return toks


def _clean_sent(s):
    s = re.sub(r"\s+", " ", s).strip()
    # 只去列表符号（- • 1. 2) 后面必须跟空格），不能把句首的年份当编号删掉
    s = re.sub(r"^\s*(?:[-–—•*·]|\d{1,2}[.)])\s+", "", s)
    return s


CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
JUNK_ZH = ("百分之", "百分", "左右", "以上", "以下", "以来")
# 数字短语先抹掉，否则切出来的都是「增长百」「分之」这类碎片
NUM_PHRASE = re.compile(r"百分之[零〇一二三四五六七八九十百千\.\d]+"
                        r"|\d+(?:\.\d+)?%"
                        r"|[零〇一二三四五六七八九]{2,}")


def _zh_overlap(a, b, n=3):
    """两词共享 n 字以上连续片段，视为同一所指的不同切法。"""
    if a == b:
        return True
    for L in range(min(len(a), len(b)), n - 1, -1):
        for i in range(len(a) - L + 1):
            if a[i:i + L] in b:
                return True
    return False


def _zh_terms(text, top=8, title=""):
    """没有分词器时怎么取中文关键词。

    核心判断：真词会重复出现，随机的 n 字窗口不会。所以只留有重复的候选，
    再用「长词优先、短词若只是长词片段就丢」去掉互相包含的冗余。
    候选不够时，从标题里补几个 3-4 字的词。
    """
    counts = {}
    for run in CJK_RUN.findall(NUM_PHRASE.sub("，", text)):
        L = len(run)
        for n in (2, 3, 4, 5, 6):
            for i in range(L - n + 1):
                g = run[i:i + n]
                if g in STOP_ZH or any(j in g for j in JUNK_ZH):
                    continue
                # 首尾是虚词的多半是跨词切出来的碎片
                if g[0] in "的了和与及或在是对将把被从" or g[-1] in "的了和与及或在是对将把被从":
                    continue
                counts[g] = counts.get(g, 0) + 1

    def pick(min_count, lengths):
        keep = {}
        for g, c in sorted(counts.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
            if c < min_count or len(g) not in lengths:
                continue
            if any(g in k and c <= kc for k, kc in keep.items()):
                continue
            keep[g] = c
        return sorted(keep.items(), key=lambda kv: -(kv[1] * (len(kv[0]) ** 1.2)))

    ranked = pick(2, (2, 3, 4, 5, 6))
    if len(ranked) < 4:
        have = {g for g, _ in ranked}
        for g, c in pick(1, (3, 4)):
            if g not in have and title and g in title:
                ranked.append((g, c))

    out = []
    for g, _ in ranked:
        if any(_zh_overlap(g, o) for o in out):
            continue
        out.append(g)
        if len(out) >= top:
            break
    return out


def local_summary(blocks, title="", max_sents=4, ratio=0.22):
    """返回 (摘要文本, 关键词列表)。纯离线。"""
    sents = []
    for tag, text in blocks or []:
        if tag in ("li",):
            continue
        head = (text or "").strip()
        if len(head) < 60 and REF_HEAD.match(head):
            break  # 到参考文献列表就停，后面的不是正文
        for s in _sent_split(text):
            s = _clean_sent(s)
            if len(s) < 25:
                continue
            if len(CJK.findall(s)) < 8 and len(WORD_EN.findall(s)) < 6:
                continue
            if _is_cite(s):
                continue
            sents.append(s)
    if not sents:
        return "", []

    # 摘要长度预算：长文给 900 字符左右，短文按比例缩
    total = sum(len(s) for s in sents)
    if total > 6000:
        budget = 900
    elif total >= 1200:
        budget = max(320, int(total * 0.15))
    else:
        budget = max(180, int(total * 0.35))
    budget = min(budget, 1200)
    minsent = 3 if total >= 1200 else 2

    tok_sets = [set(_tokens(s)) for s in sents]
    df = {}
    for ts in tok_sets:
        for t in ts:
            df[t] = df.get(t, 0) + 1
    n = len(sents)
    idf = {t: math.log(1.0 + n / (1.0 + c)) for t, c in df.items()}

    title_toks = set(_tokens(title))
    scores = []
    for i, (s, ts) in enumerate(zip(sents, tok_sets)):
        if not ts:
            scores.append(0.0)
            continue
        w = sum(idf.get(t, 0.0) for t in ts) / math.sqrt(len(ts))
        if i < 3:
            w *= 1.18
        elif i >= n - 2:
            w *= 1.06
        if title_toks:
            ov = len(ts & title_toks) / float(len(title_toks))
            w *= 1.0 + min(0.35, ov * 0.7)
        L = len(s)
        if L > 400:
            w *= 0.6
        elif L > 300:
            w *= 0.82
        elif L < 45:
            w *= 0.75
        scores.append(w)

    order = sorted(range(n), key=lambda i: -scores[i])
    picked, acc = [], 0
    for i in order:
        if len(picked) >= max_sents:
            break
        ts = tok_sets[i]
        dup = False
        for j in picked:
            inter = len(ts & tok_sets[j])
            uni = len(ts | tok_sets[j]) or 1
            if inter / float(uni) > 0.42:
                dup = True
                break
        if dup:
            continue
        # 预算已满就收手，但至少凑够最低句数
        if len(picked) >= minsent and acc + len(sents[i]) > budget:
            continue
        picked.append(i)
        acc += len(sents[i])

    picked.sort()
    parts = []
    for i in picked:
        s = sents[i]
        if parts and not re.search(r"[。！？；]$", parts[-1]):
            parts.append(" ")
        parts.append(s)
    summary = "".join(parts)
    if len(summary) > 1500:
        cut = summary[:1500]
        k = max(cut.rfind(". "), cut.rfind("。"), cut.rfind("; "))
        summary = (cut[:k + 1] if k > 800 else cut) + "…"

    all_text = " ".join(sents)
    zh_ratio = len(CJK.findall(all_text)) / float(max(1, len(all_text)))
    if zh_ratio > 0.3:
        return summary, _zh_terms(all_text, 8)

    # 关键词：按 tf*idf 排序，取原文首次出现的写法
    first_form = {}
    for s in sents:
        for w in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", s):
            first_form.setdefault(w.lower(), w)
    tf = {}
    for ts in tok_sets:
        for t in ts:
            tf[t] = tf.get(t, 0) + 1
    ranked = sorted(tf.items(), key=lambda kv: -(kv[1] * idf.get(kv[0], 1.0)))
    kws = []
    for t, _ in ranked:
        if CJK.search(t) or t in JUNK:
            continue
        kws.append(first_form.get(t, t))
        if len(kws) >= 8:
            break
    return summary, kws


# ---------------------------------------------------------------- 大模型
PROMPT = (
    "你是专业的语料编辑。下面是一篇文章的标题和正文，请用中文写摘要。\n"
    "要求：\n"
    "1. 3 到 5 句话，只讲文章的实质内容：核心判断、关键事实与数据、结论或政策含义。\n"
    "2. 不要写「本文认为」「作者指出」这类空话开头，直接进入内容。\n"
    "3. 不要复述标题，不要评论文章好坏，不要添加原文没有的信息。\n"
    "4. 数字、机构名、人名照原文，不要改写或估算。\n"
    "5. 严格按下面两行输出，不要有别的内容：\n"
    "摘要：（正文摘要）\n"
    "关键词：词1、词2、词3、词4\n"
)


def llm_summary(blocks, title, cfg, timeout=90):
    """调用 OpenAI 兼容接口。返回 (摘要, 关键词, 模型名)，失败抛异常。"""
    body = "\n".join(t for _, t in (blocks or []))
    if len(body) > 14000:
        body = body[:14000]
    content = llm.chat(
        cfg,
        [{"role": "system", "content": PROMPT},
         {"role": "user", "content": "标题：%s\n\n正文：\n%s" % (title or "", body)}],
        temperature=0.2, timeout=timeout)

    summary, kws = _parse_llm(content)
    if not summary:
        raise llm.LLMError("模型没有返回可用摘要")
    return summary, kws, cfg.get("model", "")


def _parse_llm(content):
    content = (content or "").strip()
    kws = []
    m = re.search(r"关键词[：:]\s*(.+)", content)
    if m:
        raw = m.group(1).strip()
        kws = [k.strip() for k in re.split(r"[、,，;；]\s*", raw) if k.strip()][:8]
        content = content[:m.start()].strip()
    content = re.sub(r"^摘\s*要[：:]\s*", "", content).strip()
    return content, kws


def summarize(blocks, title, cfg=None):
    """统一入口：有大模型用大模型，失败落到本地。返回 dict。"""
    cfg = cfg or {}
    if cfg.get("enabled"):
        try:
            s, k, model = llm_summary(blocks, title, cfg)
            return {"summary": s, "keywords": k,
                    "summary_how": "大模型（%s）" % model, "engine": "llm"}
        except Exception as e:
            s, k = local_summary(blocks, title)
            return {"summary": s, "keywords": k,
                    "summary_how": "本地提取式（大模型调用失败：%s）" % str(e)[:80],
                    "engine": "local"}
    s, k = local_summary(blocks, title)
    return {"summary": s, "keywords": k, "summary_how": "本地提取式", "engine": "local"}

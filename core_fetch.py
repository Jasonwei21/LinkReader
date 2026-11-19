# -*- coding: utf-8 -*-
"""语料抓取核心库：抓取 → 订阅墙识别 → 正文分段 → 元数据。

设计原则
1. 只抓公开可读内容。识别到订阅墙一律标记并跳过，不做任何绕过。
2. 正文抽取不依赖 DOM 嵌套深度，避免错标签（cookie 弹窗常把正文包错层）导致整块丢失。
3. 所有判定都要能复述理由：每条记录都带 reason 与 evidence，便于人工复核。
"""
import gzip
import hashlib
import html as htmlmod
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser

UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"),
]

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# 以订阅制为主的站点：只作为"提高警惕"的提示，不直接判死，仍以正文实测为准
HARD_HOSTS = {
    "ft.com", "wsj.com", "barrons.com", "economist.com", "bloomberg.com",
    "thetimes.co.uk", "telegraph.co.uk", "ftchinese.com", "caixin.com",
    "nytimes.com", "washingtonpost.com", "newyorker.com", "wired.com",
    "foreignpolicy.com", "foreignaffairs.com", "project-syndicate.org",
    "scmp.com", "latimes.com", "businessinsider.com", "theinformation.com",
}


# ---------------------------------------------------------------- 抓取
def _cache_path(cache_dir, url):
    return os.path.join(cache_dir, hashlib.md5(url.encode("utf-8")).hexdigest() + ".html")


def _decode(body, charset=None):
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    elif body[:1] == b"\x78":
        try:
            body = zlib.decompress(body)
        except Exception:
            pass
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "ignore")


def fetch(url, cache_dir="cache", timeout=40, retries=3, use_cache=True):
    """返回 (raw_html, info)。info: status / note / from_cache。"""
    info = {"status": None, "note": "", "from_cache": False, "final_url": url}
    if url.lower().endswith(".pdf"):
        return None, dict(info, note="PDF 直链，交由 PDF 解析")

    fn = _cache_path(cache_dir, url)
    if use_cache and os.path.exists(fn) and os.path.getsize(fn) > 3000:
        return open(fn, encoding="utf-8", errors="ignore").read(), dict(info, from_cache=True)

    last = ""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UAS[i % len(UAS)],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.6",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                charset = None
                try:
                    charset = r.headers.get_content_charset()
                except Exception:
                    pass
                raw = _decode(body, charset)
                info["status"] = r.status
                info["final_url"] = r.geturl() or url
            if len(raw) > 1500:
                os.makedirs(cache_dir, exist_ok=True)
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(raw)
                return raw, info
            last = "页面过短 %d 字节" % len(raw)
        except urllib.error.HTTPError as e:
            info["status"] = e.code
            last = "HTTP %s" % e.code
            # 4xx 除 429 外不必重试
            if e.code not in (408, 429) and 400 <= e.code < 500:
                break
        except urllib.error.URLError as e:
            last = "连接失败（%s）" % str(getattr(e, "reason", e))[:60]
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:60])
        time.sleep(1.2 * (i + 1))
    info["note"] = last
    return None, info


# ---------------------------------------------------------------- 轻量 DOM
class Node(object):
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    def walk(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                for d in c.walk():
                    yield d


VOID = {"br", "img", "hr", "meta", "link", "input", "source", "area", "base",
        "col", "embed", "param", "track", "wbr"}
DROP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside",
        "form", "iframe", "button", "select", "option", "figure", "figcaption",
        "template", "dialog"}


class _Builder(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if data:
            self.cur.children.append(data)


def parse_html(raw):
    b = _Builder()
    try:
        b.feed(raw)
    except Exception:
        pass
    return b.root


JUNK_ATTR = re.compile(
    r"(cookie|consent|gdpr|newsletter|subscri|related|recommend|share-|sharing|"
    r"sidebar|breadcrumb|comment|promo|advert|banner|masthead|toolbar|popup|modal|"
    r"social|follow|paywall|signup|sign-up|teaser|news-list|nav-|-nav|menu|"
    r"pagination|outbrain|taboola|disqus|footer|header)", re.I)


def _junk_node(n):
    if n.tag in ("article", "main", "body", "html"):
        return False
    v = (n.attrs.get("class") or "") + " " + (n.attrs.get("id") or "")
    return bool(v.strip()) and bool(JUNK_ATTR.search(v))


def norm(s):
    s = htmlmod.unescape(s or "")
    s = s.replace("\u00ad", "").replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"[\t\r\f\v]+", " ", s)
    s = re.sub(r" {2,}", " ", s)
    return s.strip()


def p_text(node):
    parts = []

    def rec(n):
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag == "br":
                parts.append(" ")
            else:
                rec(c)
    rec(node)
    return norm("".join(parts))


BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "li", "blockquote", "dt", "dd", "pre")


def junk_texts(raw):
    """噪音容器（相关阅读/订阅/分享）内的文本，作为正文黑名单。"""
    root = parse_html(raw)
    out = set()
    for n in root.walk():
        if n.tag in ("article", "main", "body", "html"):
            continue
        if not _junk_node(n):
            continue
        for d in n.walk():
            if d.tag in ("p", "h1", "h2", "h3", "h4", "h5", "li", "a", "dt", "dd", "span"):
                t = p_text(d)
                if t:
                    out.add(t)
    return out


# ---------------------------------------------------------------- 正文抽取
KILL_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "header",
             "footer", "nav", "aside", "figure", "template", "select", "button")

COOKIE = re.compile(
    r"(uses? cookies? to (provide|ensure|give)|cookie information is stored|"
    r"cloudflare - cloudflare|google analytics - the google|"
    r"analytics cookies are used|functional cookies|strictly necessary cookies|"
    r"we (and our partners )?(use|store) cookies|manage (your )?(cookie )?(preferences|consent)|"
    r"this website uses cookies|accept (all )?cookies|"
    r"^(twitter|facebook|linkedin|google|cloudflare|youtube|vimeo|instagram) "
    r"- \w.* (service|network|platform))", re.I)

BOILER = re.compile(
    r"(all rights reserved|privacy policy|terms of (use|service)|sign up for|"
    r"subscribe to (our|the) newsletter|follow us on|share (this|on) |"
    r"©|copyright \d{4})", re.I)

CUT_MARK = re.compile(
    r"^(share( this| on)?|keywords?|topics?|tags?|practice area|"
    r"related( articles?| content| work| reading| publications| stories)?|"
    r"more (work|from|on|reading|stories).*|further reading|read next|read more|"
    r"recommended( for you| reading)?|about the author.*|cite this.*|"
    r"newsletter|sign up.*|in this series|explore topics|"
    r"research center|publications|events|authors?|language|"
    r"institute|contact|imprint|download|more publications|related publications|"
    r"about the authors|publication date|jel classification|key ?words|"
    r"leave a (reply|comment)|post a comment|comments?)$", re.I)

STRONG_CUT = re.compile(
    r"^(read (the |kiel |our )?.*now:?|expert|media contact|more media releases|"
    r"more publications|related publications|further reading|more work from.*|"
    r"more from .*|you may also like|recommended for you|sign up to our newsletter|"
    r"this article (was|is) (first )?(published|reprinted))$", re.I)

META_WORDS = {"news", "policy article", "working paper", "copy link", "pdf download",
              "authors", "publication date", "jel classification", "key words",
              "keywords", "commentary", "article", "report", "podcast", "press release",
              "event", "policy brief", "blog post", "op-ed", "interview", "share",
              "listen", "video", "photo", "graphic", "subscribe", "print"}

NAVWORD = re.compile(
    r"^(food|nature|pollution|ocean|business|forests|justice|water|energy|climate|"
    r"china|india|global|all|more|home|about|contact|donate|subscribe|search|"
    r"english|deutsch|中文|topics|share|print|email|twitter|x|linkedin|facebook|"
    r"menu|close|next|previous|skip to content)$", re.I)

BLOCK_RE = re.compile(r"(?is)<(p|h1|h2|h3|h4|h5|li|blockquote|dt|dd|pre)\b([^>]*)>(.*?)</\1\s*>")


def strip_inline(s, keep_breaks=False):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<br\s*/?>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = htmlmod.unescape(s)
    s = s.replace("\u00ad", "").replace("\u200b", "").replace("\ufeff", "")
    if keep_breaks:
        # <pre> 里的换行是排版本身，只能压空格和多余空行
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        s = re.sub(r" {2,}", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
    else:
        # HTML 源码里的换行只是折行排版，不是分段。留着会把一句话切成几截，
        # 到 Word 里被当成软换行，两端对齐还会把字距拉得忽宽忽窄。
        s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_regions(raw):
    for t in KILL_TAGS:
        raw = re.sub(r"(?is)<%s\b[^>]*>.*?</%s\s*>" % (t, t), " ", raw)
        raw = re.sub(r"(?is)<%s\b[^>]*/>" % t, " ", raw)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    return raw


def pick_region(raw):
    """在 <article>/<main> 中选长段落最多的一块；都不像正文则用全文。"""
    best, best_n = None, 0
    for tag in ("article", "main"):
        for m in re.finditer(r"(?is)<%s\b[^>]*>(.*?)</%s\s*>" % (tag, tag), raw):
            seg = m.group(1)
            n = sum(1 for b in BLOCK_RE.finditer(seg)
                    if len(strip_inline(b.group(3))) >= 80)
            if n > best_n:
                best, best_n = seg, n
    if best is not None and best_n >= 2:
        return best
    return raw


def blocks_from_html(raw, title="", junk=None):
    """返回 [[tag, text], ...]，按文档顺序，已去噪。"""
    raw = strip_regions(raw)
    seg = pick_region(raw)

    raw_blocks = []
    for m in BLOCK_RE.finditer(seg):
        tag = m.group(1).lower()
        txt = strip_inline(m.group(3), keep_breaks=(tag == "pre"))
        if txt:
            raw_blocks.append([tag, txt])

    junk = junk or set()
    junk_norm = set(re.sub(r"\W+", "", x.lower())[:150] for x in junk)

    blocks, seen = [], set()
    for tag, t in raw_blocks:
        if len(t) < 2:
            continue
        key = re.sub(r"\W+", "", t.lower())[:150]
        if key in junk_norm or key in seen:
            continue
        if COOKIE.search(t):
            continue
        if len(t) < 200 and BOILER.search(t):
            continue
        seen.add(key)
        blocks.append([tag, t])

    # 头部元信息（栏目标签、下载按钮、日期碎片、作者碎片）
    i = 0
    while i < len(blocks) and len(blocks[i][1]) < 200:
        tag, t = blocks[i]
        st = t.strip()
        if tag == "h1":
            i += 1
            continue
        if (re.fullmatch(r"[\d./\-]{4,12}", st) or st.lower() in META_WORDS
                or (tag == "li" and len(st.split()) <= 3 and not st.endswith("."))):
            blocks.pop(i)
            continue
        i += 1

    # 正文之后的尾巴
    start = int(len(blocks) * 0.5)
    for i, (tag, t) in enumerate(blocks):
        st = t.strip()
        if i >= 1 and tag in ("h2", "h3", "h4", "p", "dt") and STRONG_CUT.match(st):
            blocks = blocks[:i]
            break
        if i >= start and tag in ("h2", "h3", "h4", "p", "dt") and CUT_MARK.match(st):
            blocks = blocks[:i]
            break

    # 尾部导航条目
    dropped = 0
    while blocks and dropped < 30:
        tag, t = blocks[-1]
        st = t.strip()
        navish = (tag in ("li", "dt", "dd") and len(st) < 90 and
                  (NAVWORD.match(st) or not st.endswith((".", "!", "?", ":", '"', "”", ")"))))
        if tag in ("h2", "h3", "h4") and CUT_MARK.match(st):
            navish = True
        if not navish:
            break
        blocks.pop()
        dropped += 1

    # 尾部"相关阅读"式短标题（常以问号结尾，故按长度与标点双重判断）
    n = 0
    while blocks and n < 6:
        st = blocks[-1][1].rstrip()
        if len(st) < 115 and not st.endswith((".", "!", "?", ":", '"', "”", ")")):
            blocks.pop()
            n += 1
        else:
            break

    # 开头重复标题
    if blocks and title:
        tag, t = blocks[0]
        a = re.sub(r"\W+", "", t.lower())
        b = re.sub(r"\W+", "", title.lower())
        if tag in ("h1", "h2") and (a in b or b in a) and abs(len(a) - len(b)) < 30:
            blocks.pop(0)

    return blocks


def visible_text(raw):
    """整页可见文本，用于订阅墙与语种判定的兜底判断。"""
    seg = strip_regions(raw)
    seg = re.sub(r"(?s)<[^>]+>", " ", seg)
    seg = seg.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", htmlmod.unescape(seg)).strip()


# ---------------------------------------------------------------- 元数据
def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return _iso(*m.groups())
    m = re.match(r"(\d{1,2})/(\d{4})$", s)
    if m:
        return _iso(m.group(2), m.group(1), 1)
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1)[:3].lower() in MONTHS:
        return _iso(m.group(3), MONTHS[m.group(1)[:3].lower()], m.group(2))
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", s)
    if m and m.group(2)[:3].lower() in MONTHS:
        return _iso(m.group(3), MONTHS[m.group(2)[:3].lower()], m.group(1))
    return None


def _iso(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        if 1990 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
            return "%04d-%02d-%02d" % (y, m, d)
    except Exception:
        pass
    return None


def _meta(raw, *pats):
    for p in pats:
        m = re.search(p, raw, re.I | re.S)
        if m:
            v = norm(m.group(1))
            if v:
                return v
    return ""


def meta_of(raw, url="", blocks=None):
    host = ""
    m = re.match(r"https?://([^/]+)", url or "")
    if m:
        host = m.group(1).lower().replace("www.", "")

    title = _meta(
        raw,
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{3,300})',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']{3,300})',
        r'"headline"\s*:\s*"([^"]{3,300})"',
    )
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", raw)
        if h1:
            title = strip_inline(h1.group(1))
    if not title:
        t = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        if t:
            t = strip_inline(t.group(1))
            title = re.split(r"\s+[|\-–—:]\s+", t)[0].strip() or t

    author = _meta(
        raw,
        r'"author"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]{2,80})"',
        r'"author"\s*:\s*\[\s*\{[^{}]*?"name"\s*:\s*"([^"]{2,80})"',
        r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']{2,80})',
        r'<meta[^>]+property=["\']article:author["\'][^>]+content=["\']([^"\']{2,80})',
        r'class=["\'][^"\']*byline[^"\']*["\'][^>]*>(?:[^<]{0,40})<[^>]*>([^<]{2,80})',
    )

    date, date_how = "", ""
    for p, how in (
        (r'"datePublished"\s*:\s*"([^"]{6,40})', "JSON-LD"),
        (r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']{6,40})', "meta"),
        (r'<meta[^>]+content=["\']([^"\']{6,40})["\'][^>]+property=["\']article:published_time', "meta"),
        (r'<meta[^>]+name=["\'](?:datePublished|publish-date|pubdate)["\'][^>]+content=["\']([^"\']{6,40})', "meta"),
        (r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']{6,40})', "meta"),
        (r'"publishedAt"\s*:\s*"([^"]{6,40})', "JSON"),
        (r'<time[^>]*class=["\'][^"\']*date[^"\']*["\'][^>]*datetime=["\']([^"\']{8,30})', "time标签"),
    ):
        m = re.search(p, raw, re.I)
        if m:
            d = parse_date(m.group(1))
            if d:
                date, date_how = d, how
                break
    if not date:
        txt = visible_text(raw)[:6000]
        m = re.search(r"(?:Published|Posted|Updated|ver(?:ö|oe)ffentlicht)\s*(?:on|am)?\s*"
                      r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
                      r"|\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})", txt, re.I)
        if m:
            date, date_how = parse_date(m.group(1)) or "", "页面文字"

    site = _meta(raw, r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,80})')
    if site:
        # og:site_name 常带一句 slogan，取切割后的主体
        site = re.split(r"\s+[|｜–—:·]\s+", site)[0].strip() or site
        if len(site) > 45:
            site = site[:45].rsplit(" ", 1)[0]
    if not site:
        site = host

    wc = 0
    m = re.search(r'"wordCount"\s*:\s*"?(\d{2,7})', raw)
    if m:
        wc = int(m.group(1))

    locked = bool(re.search(r'"isAccessibleForFree"\s*:\s*(?:false|"false"|"False")', raw))
    tier = _meta(raw, r'<meta[^>]+property=["\']article:content_tier["\'][^>]+content=["\']([^"\']+)')
    if tier.lower() in ("locked", "paid", "premium") and not locked:
        locked = True

    return {"title": title, "author": author, "date": date, "date_how": date_how,
            "site": site, "host": host, "word_count": wc, "locked_flag": locked,
            "tier": tier}


# ---------------------------------------------------------------- 订阅墙识别
WALL_TEXT = re.compile(
    r"(subscribe to continue( reading)?|subscribe to read|to continue reading|"
    r"(this|the) (article|story|content) is (only )?(available|for) (to )?(subscribers|members)|"
    r"already a subscriber|subscribers? only|exclusive to subscribers|"
    r"sign in to (continue|read)|log ?in to (continue|read)|register to (continue|read)|"
    r"you have reached your (?:free )?(?:article|story) limit|"
    r"you've reached your limit|free articles? (?:remaining|left)|"
    r"become a (member|subscriber) to (read|continue|access)|"
    r"this (article|story) is (part of|from) our (premium|paid)|"
    r"available exclusively to|unlock (this|the full) (article|story)|"
    r"订阅后(继续)?阅读|付费阅读|会员专享|本文为付费文章)", re.I)

METER_TEXT = re.compile(
    r"(\d+\s*(?:free )?articles? (?:left|remaining)|remaining this month|"
    r"get unlimited access|start your (free )?trial)", re.I)

WALL_URL = re.compile(
    r"/(?:subscribe|subscription|signin|login|register|paywall|premium)(?:[/?#]|$)", re.I)


def detect_wall(raw, meta, blocks, status=None):
    """返回 (是否订阅墙, 类型, 依据)。只做识别，不做绕过。"""
    if status in (401, 402):
        return True, "http_auth", "HTTP %s" % status

    body_chars = len(" ".join(t for _, t in blocks))

    if meta.get("locked_flag"):
        # 少部分站点把 isAccessibleForFree:false 写死在所有页上，故用正文长度复核一次
        if body_chars < 3000:
            return True, "metadata_locked", "页面标注 isAccessibleForFree=false"
        meta["locked_flag"] = False

    text = visible_text(raw)
    head = text[:9000]

    m = WALL_TEXT.search(head)
    if m and body_chars < 1800:
        return True, "text_marker", m.group(0)[:60]

    if WALL_URL.search(meta.get("final_url", "") or "") and body_chars < 1200:
        return True, "redirect_to_auth", "被重定向到登录/订阅页"

    wc = meta.get("word_count") or 0
    if wc >= 600 and body_chars < wc * 3.2 * 0.45:
        return True, "truncated", "标注 %d 词，实取 %d 字符" % (wc, body_chars)

    if body_chars < 1200 and METER_TEXT.search(head):
        mm = METER_TEXT.search(head)
        return True, "metered", mm.group(0)[:40]

    return False, "", ""


# ---------------------------------------------------------------- 语种
STOP_EN = set("""the of and to in for on with that this from by as at is are was were be been has have
had will would can could than which its their it not but or an a we they he she you i""".split())


def guess_lang(text):
    if not text:
        return ""
    letters = len(re.findall(r"[A-Za-z]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    cyr = len(re.findall(r"[\u0400-\u04ff]", text))
    total = max(1, letters + cjk + cyr)
    if cjk / total > 0.25:
        return "zh"
    if cyr / total > 0.25:
        return "ru"
    words = re.findall(r"[A-Za-z]+", text.lower())[:400]
    if len(words) < 25:
        return "?"
    ratio = sum(1 for w in words if w in STOP_EN) / float(len(words))
    return "en" if ratio > 0.10 else "other"


NONARTICLE_URL = re.compile(
    r"/(?:about|contact|team|people|careers|jobs?|login|signin|register|privacy|"
    r"terms|cookie|sitemap|search|tag|tags|category|categories|author|authors|archive)"
    r"(?:[/?#]|$)", re.I)
NONARTICLE_TITLE = re.compile(
    r"^(?:about(?: us)?|contact|team|careers?|jobs?|search|page not found|404|"
    r"access denied|just a moment|are you a robot)", re.I)


# ---------------------------------------------------------------- PDF 兜底
def find_pdf_links(raw, base_url=""):
    """页面里指向 PDF 的链接（绝对化），用于"网页只给摘要、全文在附件"的站点。"""
    out = []
    for m in re.finditer(r'''href=["']([^"']+?\.pdf[^"']*)''', raw, re.I):
        u = htmlmod.unescape(m.group(1))
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            mm = re.match(r"(https?://[^/]+)", base_url)
            u = (mm.group(1) if mm else "") + u
        elif not u.startswith("http"):
            continue
        if u not in out:
            out.append(u)
    return out[:6]


def fetch_bytes(url, cache_dir="cache", timeout=90):
    fn = os.path.join(cache_dir, hashlib.md5(url.encode("utf-8")).hexdigest() + ".bin")
    if os.path.exists(fn) and os.path.getsize(fn) > 10000:
        return open(fn, "rb").read()
    req = urllib.request.Request(url, headers={"User-Agent": UAS[0], "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    os.makedirs(cache_dir, exist_ok=True)
    if len(data) > 10000:
        with open(fn, "wb") as f:
            f.write(data)
    return data


def pdf_blocks(data_or_path, max_pages=80):
    """PDF → [[tag, text]]。依赖 pypdf（可选）。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "未安装 pypdf"
    try:
        if isinstance(data_or_path, bytes):
            reader = PdfReader(io.BytesIO(data_or_path))
        else:
            reader = PdfReader(data_or_path)
    except Exception as e:
        return None, "PDF 解析失败：%s" % str(e)[:60]
    blocks = []
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            t = page.extract_text() or ""
        except Exception:
            continue
        for para in re.split(r"\n\s*\n", t):
            para = re.sub(r"\s+", " ", para).strip()
            if len(para) >= 40:
                blocks.append(["p", para])
    return blocks, ""


def lead_match(blocks, pdf_text, n=10):
    """页面首段 vs PDF 前部：词序重合度，用于确认 PDF 与页面是同一篇。"""
    lead = ""
    for _, t in blocks:
        if len(t) >= 80 and not re.fullmatch(r"[\d./\-\s]+", t):
            lead = t
            break
    if not lead:
        return False, 0, 0
    toks = [w for w in re.findall(r"[a-z0-9]+", lead.lower()) if len(w) > 3][:n]
    head = set(re.findall(r"[a-z0-9]+", pdf_text[:6000].lower()))
    hit = sum(1 for t in toks if t in head)
    return (len(toks) >= 5 and hit >= max(5, len(toks) - 1)), hit, len(toks)

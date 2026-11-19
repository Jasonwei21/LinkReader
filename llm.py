# -*- coding: utf-8 -*-
"""OpenAI 兼容接口的最小封装。摘要和翻译共用这一个出口。"""
import json
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


def configured(cfg):
    cfg = cfg or {}
    return bool(cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"))


def chat(cfg, messages, temperature=0.2, timeout=120, max_tokens=None):
    """发一次对话请求，返回文本内容。失败抛 LLMError（带可读原因）。"""
    if not configured(cfg):
        raise LLMError("大模型配置不完整（需要 base_url / api_key / model）")
    base = (cfg.get("base_url") or "").rstrip("/")
    # 允许用户直接填到 /chat/completions
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"

    payload = {"model": cfg["model"], "temperature": temperature, "messages": messages}
    if max_tokens:
        payload["max_tokens"] = max_tokens

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        if e.code == 401:
            raise LLMError("API Key 无效或过期（401）")
        if e.code == 404:
            raise LLMError("接口地址或模型名不对（404）：%s" % detail[:160])
        if e.code == 429:
            raise LLMError("触发限流或额度不足（429）")
        raise LLMError("接口返回 HTTP %s：%s" % (e.code, detail[:200]))
    except urllib.error.URLError as e:
        raise LLMError("连不上接口：%s" % e.reason)
    except Exception as e:
        raise LLMError("请求失败：%s" % e)

    try:
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        raise LLMError("返回结构异常：%s" % str(data)[:200])


def strip_fence(s):
    """去掉 ```json ... ``` 这种包裹。"""
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        s = s.lstrip("`")
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()

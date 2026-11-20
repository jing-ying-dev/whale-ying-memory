# -*- coding: utf-8 -*-
"""deepseek_api: a minimal LLM call adapter.

The extractor needs a stable `deepseek_api.call(prompt)` interface, but no such module
existed in this workspace and it could not be imported (ModuleNotFoundError). This file
provides a small, self-contained implementation so `extract.py` can depend on one fixed
calling surface.

Configuration (all via environment variables; no secrets are hard-coded or written to disk):
  DEEPSEEK_API_PROVIDER  deepseek (default) | ollama
  DEEPSEEK_API_BASE      default https://api.deepseek.com ; ollama default http://127.0.0.1:11434/v1
  DEEPSEEK_API_KEY       required (provider=deepseek); optional for provider=ollama
  DEEPSEEK_MODEL         default deepseek-flash ; ollama default qwen2.5-coder:7b

Public interface:
  call(prompt, **kw) -> dict{"text","model","endpoint","usage","elapsed_ms","finish_reason"}
  probe() -> dict{"configured","reason","provider","base","model","reachable",...}
  DeepSeekAPIUnavailable / DeepSeekAPITruncated

When finish_reason == "length", raise DeepSeekAPITruncated directly: truncation and
"the model wrote garbage" must be reported separately, otherwise waiting for JSON
parsing to fail would conflate the two distinct causes.

When unconfigured or unreachable, raise DeepSeekAPIUnavailable; the error message
states exactly which environment variable is missing. Never fabricate a response
or silently degrade into fake data.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = {"deepseek": "https://api.deepseek.com", "ollama": "http://127.0.0.1:11434/v1"}
DEFAULT_MODEL = {"deepseek": "deepseek-flash", "ollama": "qwen2.5-coder:7b"}
KEY_ENV = "DEEPSEEK_API_KEY"
BASE_ENV = "DEEPSEEK_API_BASE"
PROVIDER_ENV = "DEEPSEEK_API_PROVIDER"
MODEL_ENV = "DEEPSEEK_MODEL"


class DeepSeekAPIUnavailable(RuntimeError):
    """Call surface unavailable: missing config / unreachable / non-200 response. The message is safe to show to a user directly."""


class DeepSeekAPITruncated(DeepSeekAPIUnavailable):
    """Output truncated by max_tokens (finish_reason == "length"). Must be distinguished from "model wrote malformed JSON"."""


def load_config():
    provider = (os.environ.get(PROVIDER_ENV) or "deepseek").strip().lower()
    if provider not in DEFAULT_BASE:
        provider = "deepseek"
    base = (os.environ.get(BASE_ENV) or DEFAULT_BASE[provider]).strip().rstrip("/")
    model = (os.environ.get(MODEL_ENV) or DEFAULT_MODEL[provider]).strip()
    key = (os.environ.get(KEY_ENV) or "").strip()
    if provider == "deepseek" and not key:
        reason = "未配置 %s（provider=deepseek），无法调用 %s" % (KEY_ENV, base)
    else:
        reason = ""
    return {"provider": provider, "base": base, "model": model, "key": key,
            "configured": not reason, "reason": reason}


def _post_json(url, payload, headers, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def call(prompt, model=None, temperature=0.0, max_tokens=None, timeout=600,
         response_format_json=True, repair=None, disable_thinking=False):
    """Send one chat/completions request and return {"text": <assistant reply verbatim>, ...}.

    `prompt` is a single user message; prepend any system instruction into `prompt`
    yourself (to keep the calling surface minimal).

    `repair` is used by extract.py for a second-chance retry. Its shape is
    {"reason": "<why the previous output was rejected>", "attempt": 2}; when given,
    an extra user message tells the model why the previous output was rejected, which
    helps it escape the same mistake more reliably than simply resending the identical
    prompt — the retry works because of this feedback, not by luck.
    """
    cfg = load_config()
    if not cfg["configured"]:
        raise DeepSeekAPIUnavailable(cfg["reason"])
    use_model = (model or cfg["model"]).strip()
    if max_tokens is None:
        max_tokens = int(os.environ.get("DEEPSEEK_MAX_TOKENS") or "4096")
    messages = [{"role": "user", "content": prompt}]
    if repair:
        reason = str((repair or {}).get("reason") or "输出不合契约")
        attempt = (repair or {}).get("attempt")
        head = "上一次输出被拒绝（第 %s 次尝试）" % attempt if attempt else "上一次输出被拒绝"
        messages.append({"role": "user", "content": (
            "%s：%s\n请按契约修正后重新输出**完整** JSON 对象"
            "（cards / metrics / identities / facts 四个键），不要解释、不要 markdown 代码围栏。" %
            (head, reason))})
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    headers = {"Content-Type": "application/json"}
    if cfg["key"]:
        headers["Authorization"] = "Bearer " + cfg["key"]
    url = cfg["base"] + "/chat/completions"
    t0 = time.time()
    # Retry on transient network errors: up to 3 attempts for connect failures / read timeouts.
    for attempt in range(3):
        try:
            status, text = _post_json(url, payload, headers, timeout)
            break
        except urllib.error.HTTPError as exc:  # 4xx/5xx: surface the response body, no retry
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise DeepSeekAPIUnavailable("HTTP %s from %s: %s" % (exc.code, url, detail)) from exc
        except Exception as exc:  # noqa: BLE001 connect failure / timeout / DNS
            if attempt < 2:
                print("deepseek_api retry %d/3 after %s: %s"
                      % (attempt + 1, type(exc).__name__, str(exc)[:120]), file=sys.stderr)
                time.sleep(10)
                continue
            raise DeepSeekAPIUnavailable("连不上 %s：%s: %s"
                                         % (url, type(exc).__name__, exc)) from exc
    elapsed_ms = int((time.time() - t0) * 1000)
    try:
        doc = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise DeepSeekAPIUnavailable("返回不是 JSON（HTTP %s）：%s" % (status, text[:300])) from exc
    try:
        choice = doc["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekAPIUnavailable("返回缺 choices[0].message.content：%s"
                                     % (text[:300],)) from exc
    try:
        content = choice["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise DeepSeekAPIUnavailable("返回缺 choices[0].message.content：%s"
                                     % (text[:300],)) from exc
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        usage = doc.get("usage") or {}
        raise DeepSeekAPITruncated(
            "输出被 max_tokens 截断（finish_reason=length，max_tokens=%s，completion_tokens=%s）："
            "先降 chunk 长度/拆片，再调大 max_tokens；不是模型写坏 JSON"
            % (max_tokens, usage.get("completion_tokens")))
    return {"text": content, "model": doc.get("model", use_model), "endpoint": url,
            "usage": doc.get("usage"), "elapsed_ms": elapsed_ms, "http_status": status,
            "finish_reason": finish_reason}


def probe(timeout=5):
    """Probe liveness without raising: record whether a real call would work right now."""
    cfg = load_config()
    out = {"configured": cfg["configured"], "reason": cfg["reason"],
           "provider": cfg["provider"], "base": cfg["base"], "model": cfg["model"],
           "reachable": False, "detail": ""}
    if not cfg["configured"]:
        return out
    try:
        req = urllib.request.Request(cfg["base"] + "/models",
                                     headers={"Authorization": "Bearer " + cfg["key"]})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["reachable"] = resp.status == 200
            out["detail"] = "GET /models -> HTTP %s" % resp.status
    except Exception as exc:  # noqa: BLE001
        out["detail"] = "%s: %s" % (type(exc).__name__, exc)
    return out


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))

# -*- coding: utf-8 -*-
"""deepseek_api: LLM 调用适配器（task-61 新建，交付于 runs/61/）。

背景：任务书说「调 deepseek_api.call（你 task-56 用过）」，但本工作区没有该模块，
python 环境里也 import 不到（ModuleNotFoundError），task-56 实际是联网调研、从未调过 LLM。
为了让 extract.py 的调用面固定成 `deepseek_api.call(prompt)`，这里补一个最小实现。

配置（全部走环境变量，不落盘、不写死 key）：
  DEEPSEEK_API_PROVIDER  deepseek（默认）| ollama
  DEEPSEEK_API_BASE      默认 https://api.deepseek.com ；ollama 默认 http://127.0.0.1:11434/v1
  DEEPSEEK_API_KEY       必填（provider=deepseek）；provider=ollama 可空
  DEEPSEEK_MODEL         默认 deepseek-flash ；ollama 默认 qwen2.5-coder:7b

对外：
  call(prompt, **kw) -> dict{"text","model","endpoint","usage","elapsed_ms","finish_reason"}
  probe() -> dict{"configured","reason","provider","base","model","reachable",...}
  DeepSeekAPIUnavailable / DeepSeekAPITruncated

task-63 ③：finish_reason == "length" 时直接抛 DeepSeekAPITruncated（截断与「模型乱写」
必须分开报；等 JSON 解析失败再报会把两种病因混成一个）。

未配置/连不上时抛 DeepSeekAPIUnavailable，错误信息里直说缺哪个环境变量，
不伪造响应、不静默降级成假数据。
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
    """调用面不可用：缺配置 / 连不上 / 返回非 200。消息可直接给人看。"""


class DeepSeekAPITruncated(DeepSeekAPIUnavailable):
    """输出被 max_tokens 截断（finish_reason == "length"）。必须与「模型乱写 JSON」分开。"""


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
    """发一次 chat/completions 请求，返回 {"text": 助手回复原文, ...}。

    prompt 为单条 user 消息；要用 system 提示请自己拼进 prompt（保持调用面最小）。

    repair：task-64 B 档重试用（extract.py 调用）。形如 {"reason": "<该片被拒原因>",
    "attempt": 2}；给了就追加一条 user 消息把「上一次输出为什么被拒」告诉模型，
    比原样重发同一 prompt 更容易跳出同一个错——重试靠的是这条反馈，不是碰运气。
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
    # 网络抖动重试：连接失败/读超时最多重试 3 次（手机热点/USB 共享常见瞬态失败）
    for attempt in range(3):
        try:
            status, text = _post_json(url, payload, headers, timeout)
            break
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也把响应体带出来，不重试
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise DeepSeekAPIUnavailable("HTTP %s from %s: %s" % (exc.code, url, detail)) from exc
        except Exception as exc:  # noqa: BLE001 连接失败/超时/DNS
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
    """只探活，不抛异常：给留痕/自测记录「当前能不能真调」。"""
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

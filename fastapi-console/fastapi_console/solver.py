"""Server-side LLM client for plan-only assistance.

The console is deliberately not a second solver. It may ask an LLM to turn a
challenge brief into a safe investigation plan, but it must never turn model
text into a flag candidate or submit it to the benchmark platform.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

from tsecbench.errors import APIError

SYSTEM_PROMPT = (
    "你是资深 CTF / 渗透测试解题助手。你只能基于给定的题目信息、目标地址和提示，"
    "制定一份通用、可验证的调查计划。\n"
    "严格禁止输出 flag、flag 候选、答案内容、猜测的密钥或可直接提交给平台的字符串。\n"
    "请按以下结构输出简洁 JSON 对象：summary（判断依据）、evidence_needed（还需从目标取得的证据）、"
    "next_steps（按成本排序的具体操作）。无法判断时明确说明缺少什么现场证据。\n"
    "不要把题目描述、题号或历史答案当作已知解法；所有结论都必须等待靶场中的实际证据验证。"
)

# Plan output and provider diagnostics are untrusted.  Match the
# platform-shaped envelope even when a model separates the word's letters or
# pretty-prints the body across several lines.  ``[^}]`` deliberately includes
# newlines; the bounded body prevents it from swallowing an unbounded response
# on malformed output.
FLAG_LIKE_RE = re.compile(
    r"f\s*l\s*a\s*g\s*\{\s*[^}]{1,200}?\s*\}", re.IGNORECASE
)


def redact_flag_like_text(text: object) -> str:
    """Make accidental answer-shaped text safe to return to the browser."""
    safe = FLAG_LIKE_RE.sub("[已脱敏的疑似 flag]", str(text or ""))
    return safe.strip()[:12000]


def _safe_provider_detail(value: object, fallback: str) -> str:
    """Never reflect a provider's raw diagnostic into a browser response."""
    return redact_flag_like_text(value) or fallback


def ask_llm(cfg: dict, messages: list[dict]) -> str:
    """调用 OpenAI 兼容接口；cfg 来自会话配置。空响应自动重试 2 次。"""
    base = (cfg.get("llmBaseUrl") or "").strip().rstrip("/")
    if not base:
        raise APIError(400, "llm_config_missing", "未配置 LLM Base URL")
    if not (cfg.get("llmApiKey") or "").strip():
        raise APIError(400, "llm_config_missing", "未配置 LLM API Key")
    if not (cfg.get("llmModel") or "").strip():
        raise APIError(400, "llm_config_missing", "未配置 LLM 模型")

    body = {
        "model": cfg["llmModel"].strip(),
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    if cfg.get("llmThinking"):
        body["thinking"] = {"type": "enabled"}
        if cfg.get("llmReasoningEffort"):
            body["reasoning_effort"] = cfg["llmReasoningEffort"]

    last_detail = ""
    for attempt in range(3):
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['llmApiKey'].strip()}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
                if isinstance(error_body, dict):
                    error = error_body.get("error", {})
                    detail = error.get("message", "") if isinstance(error, dict) else error
            except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            last_detail = _safe_provider_detail(detail or exc.reason, "LLM 接口返回了错误")
            if exc.code == 429 and attempt < 2:
                continue
            raise APIError(exc.code, "llm_error", f"LLM 接口错误: {last_detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            last_detail = _safe_provider_detail(exc, "LLM 网络请求失败")
            if attempt < 2:
                continue
            raise APIError(503, "llm_unreachable", f"LLM 请求失败: {last_detail}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Provider output can be malformed or transformed into a
            # non-OpenAI-compatible shape.  Do not include its raw body in a
            # control-plane error, and retry just like an empty completion.
            last_detail = "LLM 返回了无法解析的响应"
            if attempt < 2:
                continue
            raise APIError(502, "llm_invalid_response", last_detail) from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = ""
        if not isinstance(content, str):
            # Some compatible providers return an array of tool/content parts.
            # This console has a text-only, plan-only contract; reflecting an
            # arbitrary transformed object could become an answer channel.
            last_detail = "LLM 返回了非文本计划"
            continue
        if content.strip():
            return content
        last_detail = "空响应"
    raise APIError(502, "llm_empty", f"LLM 连续返回空内容（{last_detail}），已重试 3 次")

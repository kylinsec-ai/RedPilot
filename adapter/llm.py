"""
LLM 客户端 — 验证器侧模型调用

支持 OpenAI 兼容 API 和智谱 (zai) API。
"""

from __future__ import annotations

import logging
import time

from .throttle import RateLimiter

log = logging.getLogger("adapter.llm")


class _LLMResponse:
    """统一的 LLM 响应"""
    def __init__(self, text: str = "", reasoning_text: str = "",
                 completion_tokens: int = 0):
        self.text = text
        self.content = text
        self.reasoning_text = reasoning_text
        self.completion_tokens = completion_tokens


class LLMClient:
    """通用 LLM 客户端"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        self._limiter = RateLimiter(cfg.min_interval)
        self._init_client()

    def _init_client(self):
        provider = self.cfg.provider.lower()
        if provider in ("zai", "zhipu", "glm"):
            self._init_zhipu()
        else:
            self._init_openai()

    def _init_openai(self):
        try:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self.cfg.base_url,
                api_key=self.cfg.api_key,
                timeout=self.cfg.timeout,
            )
            self._provider_type = "openai"
        except ImportError:
            log.warning("openai package not installed")
            raise

    def _init_zhipu(self):
        try:
            from zhipuai import ZhipuAI
            self._client = ZhipuAI(api_key=self.cfg.api_key)
            self._provider_type = "zhipu"
        except ImportError:
            try:
                import zhipuai
                self._client = zhipuai
                zhipuai.api_key = self.cfg.api_key
                self._provider_type = "zhipu_legacy"
            except ImportError:
                # 降级为 OpenAI 兼容模式
                log.info("zhipuai not installed, using openai-compatible for glm")
                self._init_openai()

    def chat(self, messages: list, *, max_tokens: int = None,
             thinking: bool = None, model: str = None) -> _LLMResponse:
        """
        发送聊天请求。

        返回 _LLMResponse 包含 text 和 reasoning_text。
        """
        self._limiter.wait()
        _model = model or self.cfg.model
        _max = max_tokens or self.cfg.max_tokens
        _thinking = thinking if thinking is not None else self.cfg.thinking

        for attempt in range(self.cfg.empty_retries + 1):
            try:
                resp = self._call(_model, messages, _max, _thinking)
                if resp.text.strip():
                    return resp
                if attempt < self.cfg.empty_retries:
                    log.warning("empty response from %s, retry %d", _model, attempt + 1)
                    time.sleep(1)
            except Exception as e:
                if attempt < self.cfg.empty_retries:
                    log.warning("LLM call error: %s, retry %d", e, attempt + 1)
                    time.sleep(2)
                else:
                    raise

        return _LLMResponse()

    def _call(self, model: str, messages: list, max_tokens: int,
              thinking: bool) -> _LLMResponse:
        """实际 API 调用"""
        try:
            kwargs = dict(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=self.cfg.temperature,
            )
            if thinking:
                kwargs["thinking"] = {"type": "enabled"}
                if getattr(self.cfg, "reasoning_effort", ""):
                    kwargs["reasoning_effort"] = self.cfg.reasoning_effort
            resp = self._client.chat.completions.create(**kwargs)
            choice = resp.choices[0] if resp.choices else None
            if choice is None:
                return _LLMResponse()

            text = choice.message.content or ""
            reasoning = ""
            tokens = getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0

            # 尝试提取 reasoning
            if hasattr(choice.message, "reasoning_content"):
                reasoning = choice.message.reasoning_content or ""

            return _LLMResponse(text=text, reasoning_text=reasoning,
                                completion_tokens=tokens)
        except Exception as e:
            log.error("LLM API call failed: %s", e)
            raise

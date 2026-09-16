"""
LLM 客户端 — 验证器侧模型调用

支持 OpenAI 兼容 API 和智谱 (zai) API。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

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
        self._last_call = 0.0
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

    def _rate_limit(self):
        if self.cfg.min_interval > 0:
            now = time.monotonic()
            delta = now - self._last_call
            if delta < self.cfg.min_interval:
                time.sleep(self.cfg.min_interval - delta)
            self._last_call = time.monotonic()

    def chat(self, messages: list, *, max_tokens: int = None,
             thinking: bool = None, model: str = None) -> _LLMResponse:
        """
        发送聊天请求。

        返回 _LLMResponse 包含 text 和 reasoning_text。
        """
        self._rate_limit()
        _model = model or self.cfg.model
        _max = max_tokens or self.cfg.max_tokens
        _thinking = thinking if thinking is not None else self.cfg.thinking

        _stopped_early = False
        for attempt in range(self.cfg.empty_retries + 1):
            try:
                resp = self._call(_model, messages, _max, _thinking)
                if resp.text.strip():
                    return resp
                # [B53] 正文空 ≠ 模型没答。推理模型的思维链与正文**共享** max_tokens，
                # 思维链吃光预算时正文必然为空（实测 200 预算下 6/6）。
                # 以前这两种情形在日志上完全同形，会把排查引向「模型失联」的错方向。
                _rs = (getattr(resp, "reasoning_text", "") or "").strip()
                _ct = resp.completion_tokens or 0
                if _rs:
                    log.warning(
                        "正文为空但思维链有 %d 字符 (completion_tokens=%d) —— "
                        "疑似 max_tokens=%d 被思维链耗尽，非模型失联",
                        len(_rs), _ct, _max)
                # [B65b] 预算已撞顶 ⇒ **原样重试是同题同解**，直接停。
                # 同样 prompt + 同样上限 → 同一份量级的思维链照样吃光预算；实测
                # 5 次尝试 5 次空、每次约 18s、每次约 4k tokens，而观察者的 join
                # 熔断只有 45s —— 前 2 次没跑完就「超时」了，剩下的在游离线程里
                # 继续烧，日志上却只剩一句「观察者超时」，真因完全不可见。
                # ★ 只掐这一种形态：偶发空响应（无思维链 / tokens 没撞顶）照旧
                #   走重试，B47c「买判断 Agent 在场率」的收益不受影响。
                if _rs and _ct >= _max:
                    log.warning(
                        "empty response from %s: 思维链已吃满预算 (%d/%d)，"
                        "原样重试只会得到同一份思维链 → 提前停止重试",
                        _model, _ct, _max)
                    _stopped_early = True
                    break
                if attempt < self.cfg.empty_retries:
                    log.warning("empty response from %s, retry %d", _model, attempt + 1)
                    time.sleep(1)
            except Exception as e:
                if attempt < self.cfg.empty_retries:
                    log.warning("LLM call error: %s, retry %d", e, attempt + 1)
                    time.sleep(2)
                else:
                    raise

        # 走到这里 = **最后一次**尝试也返回了空文本（异常在最后一次会 raise 出去）。
        # 旧代码在此静默返回空对象：retry 日志被 `attempt < empty_retries` 挡住，
        # 最后一次的空响应在日志上**没有任何痕迹**，调用方只能看到「没有裁决」——
        # 于是「模型失联」与「模型没意见」变得无法区分（2026-09-11 现场实测）。
        # 提前停与「重试跑满」是两种不同的现场，日志必须分开 —— B53 那次就是
        # 因为两种情形在日志上同形，把排查引向了「模型失联」的错方向。
        if _stopped_early:
            log.warning("empty response from %s: 预算耗尽，已提前停止重试"
                        "（未跑满 %d 次），返回空响应",
                        _model, self.cfg.empty_retries + 1)
        else:
            log.warning("empty response from %s: 累计 %d 次尝试全为空，返回空响应",
                        _model, self.cfg.empty_retries + 1)
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

"""provider / 模型可配置性回归。

以前 provider 名与凭据 env 名写死在三处（`--model` 前缀、`models.json` 的
providers 键、`$DEEPSEEK_API_KEY`），换 provider 要改代码。现在收敛到
`ADAPTER_PROVIDER` / `SOLVER_PROVIDER` + `SOLVER_MODEL` 两个配置项。

本文件锁的是这三条不变量：
  1. provider 解析优先级 显式 > SOLVER_PROVIDER > ADAPTER_PROVIDER > deepseek；
  2. 模型自带 `<provider>/` 前缀时**前缀优先**（否则 `--model` 与 models.json
     的 provider 键会不一致 → pi 找不到路由）；
  3. `_write_pi_models` 写出的 provider 键 / apiKey env 与解析结果一致。
"""

from __future__ import annotations

import json
import os

import pytest

from redpilot.worker.adapter.solver import pi_agent as eng

_ENV_KEYS = ("ADAPTER_PROVIDER", "SOLVER_PROVIDER", "SOLVER_MODEL", "ANTHROPIC_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


# ── provider 解析优先级 ──

def test_provider_default_is_deepseek():
    assert eng.resolve_provider() == "deepseek"


def test_provider_env_precedence(monkeypatch):
    monkeypatch.setenv("ADAPTER_PROVIDER", "glm")
    assert eng.resolve_provider() == "glm"
    monkeypatch.setenv("SOLVER_PROVIDER", "anthropic")   # 更具体的别名优先
    assert eng.resolve_provider() == "anthropic"
    assert eng.resolve_provider("openai") == "openai"    # 显式参数最高


def test_provider_name_is_normalized(monkeypatch):
    monkeypatch.setenv("ADAPTER_PROVIDER", "  OpenRouter ")
    assert eng.resolve_provider() == "openrouter"


# ── 画像与凭据 env ──

def test_known_provider_profile():
    prof = eng.provider_profile("anthropic")
    assert prof["api"] == "anthropic-messages"
    assert prof["api_key_env"] == "ANTHROPIC_API_KEY"


def test_unknown_provider_gets_derived_key_env():
    """换到仓库没登记的 provider 也应能跑：兜底画像 + <PROVIDER>_API_KEY。"""
    prof = eng.provider_profile("my-gw")
    assert prof["api"] == "openai-completions"
    assert prof["api_key_env"] == "MY_GW_API_KEY"


def test_deepseek_profile_keeps_its_compat():
    """deepseek 的 reasoningContent/thinkingFormat 兼容块不能丢。"""
    prof = eng.provider_profile("deepseek")
    assert prof["compat"]["thinkingFormat"] == "deepseek"
    assert prof["compat"]["requiresReasoningContentOnAssistantMessages"] is True
    assert prof["api_key_env"] == "DEEPSEEK_API_KEY"


# ── 模型规范化 / 前缀优先 ──

def test_normalize_model_prepends_configured_provider(monkeypatch):
    monkeypatch.setenv("ADAPTER_PROVIDER", "glm")
    assert eng.normalize_model("mimo-v2.5") == "glm/mimo-v2.5"


def test_split_model_prefix_wins_over_configured_provider(monkeypatch):
    """`SOLVER_MODEL=openai/gpt-5` 时 provider 必须变成 openai。

    否则 models.json 会按 ADAPTER_PROVIDER 写成 `deepseek` 键，
    而 --model 说 openai → pi 找不到路由（静默 0 turns 那一类）。
    """
    monkeypatch.setenv("ADAPTER_PROVIDER", "deepseek")
    prov, mid = eng.split_model("openai/gpt-5", "deepseek")
    assert (prov, mid) == ("openai", "gpt-5")


def test_split_model_bare_name_uses_configured_provider(monkeypatch):
    monkeypatch.setenv("ADAPTER_PROVIDER", "glm")
    assert eng.split_model("mimo-v2.5", "glm") == ("glm", "mimo-v2.5")


# ── 落盘：provider 键 / apiKey 必须与解析一致 ──

def _write(tmp_path, model, provider):
    eng._write_pi_models(str(tmp_path), base_url="http://gw.example",
                         model=model, provider=provider)
    path = os.path.join(str(tmp_path), ".pi", "agent", "models.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["providers"]


def test_write_models_uses_configured_provider_and_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTER_PROVIDER", "glm")
    provs = _write(tmp_path, "glm/mimo-v2.5", "glm")
    assert set(provs) == {"glm"}
    assert provs["glm"]["apiKey"] == "$GLM_API_KEY"
    assert provs["glm"]["baseUrl"] == "http://gw.example"
    assert provs["glm"]["models"][0]["id"] == "mimo-v2.5"


def test_write_models_prefix_overrides_provider(tmp_path):
    provs = _write(tmp_path, "openai/gpt-5", "deepseek")
    assert set(provs) == {"openai"}
    assert provs["openai"]["apiKey"] == "$OPENAI_API_KEY"
    assert provs["openai"]["models"][0]["id"] == "gpt-5"


def test_write_models_custom_provider(tmp_path):
    provs = _write(tmp_path, "mygw/my-model", "mygw")
    assert set(provs) == {"mygw"}
    assert provs["mygw"]["apiKey"] == "$MYGW_API_KEY"


def test_write_models_deepseek_keeps_compat_block(tmp_path):
    provs = _write(tmp_path, "deepseek/mimo-v2.5", "deepseek")
    entry = provs["deepseek"]["models"][0]
    assert entry["compat"]["thinkingFormat"] == "deepseek"
    assert entry["contextWindow"] == 1000000

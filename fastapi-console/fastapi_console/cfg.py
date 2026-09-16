"""FastAPI 控制台配置（会话级），与 Django 版 tsecweb/session_cfg.py 对齐。"""

DEFAULTS = {
    "baseUrl": "",
    "token": "",
    "llmBaseUrl": "https://api.deepseek.com",
    "llmApiKey": "",
    "llmModel": "deepseek-v4-flash",
    "llmThinking": False,
    "llmReasoningEffort": "medium",
    "useHint": False,
    "maxRounds": 6,
    "autoClose": True,
}


def get_cfg(session: dict) -> dict:
    saved = session.get("console_cfg", {})
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    cfg["llmThinking"] = bool(cfg["llmThinking"])
    cfg["useHint"] = bool(cfg["useHint"])
    cfg["autoClose"] = bool(cfg["autoClose"])
    try:
        cfg["maxRounds"] = max(1, min(50, int(cfg["maxRounds"])))
    except (TypeError, ValueError):
        cfg["maxRounds"] = DEFAULTS["maxRounds"]
    return cfg


def save_cfg(session: dict, data: dict) -> dict:
    cfg = get_cfg(session)
    for key in DEFAULTS:
        if key in data:
            cfg[key] = data[key]
    session["console_cfg"] = cfg
    return cfg


def remote_config(session: dict) -> tuple[str, str] | None:
    """会话里配置了远端平台则返回 (base_url, token)，否则 None（本地模式）。"""
    cfg = get_cfg(session)
    base = (cfg.get("baseUrl") or "").strip().rstrip("/")
    token = (cfg.get("token") or "").strip()
    if base and token:
        return base, token
    return None
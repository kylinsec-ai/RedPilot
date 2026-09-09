# ══════════════════════════════════════════════════════════════
# TsecBench 平台接入层适配器 Dockerfile
# 基于已构建的 TsecBench Kali 镜像，叠加 Pi Agent + 适配器代码
# ══════════════════════════════════════════════════════════════

FROM tsecbench/kali:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_VERSION=20.18.1 \
    LANG=C.UTF-8

# ── 1. 配置国内镜像源 (pip) ──
RUN printf '[global]\nindex-url = https://mirrors.bfsu.edu.cn/pypi/web/simple\ntimeout = 120\n[install]\ntrusted-host = mirrors.bfsu.edu.cn\n' \
        > /etc/pip.conf

# ── 2. 安装 Node.js + Pi Agent ──
RUN set -eux; arch="$(uname -m)"; case "$arch" in x86_64) NA=x64;; aarch64|arm64) NA=arm64;; *) NA=x64;; esac; \
    wget -q -O /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NA}.tar.xz"; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; rm -f /tmp/node.tar.xz; \
    node --version; \
    npm install -g --ignore-scripts @earendil-works/pi-coding-agent; \
    pi --version || true

# ── 2.1 TsecBench 框架护栏: 给 pi 内置 bash 工具打默认超时+ulimit+重复短路补丁 ──
# （adapter/pi_ext/patch_pi_bash.py 也 bind-mount 进容器，运行中可随时就地重打）
COPY adapter/pi_ext/patch_pi_bash.py /app/adapter/pi_ext/patch_pi_bash.py
RUN python3 /app/adapter/pi_ext/patch_pi_bash.py && \
    node --check /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/bash.js

# ── 3. Pi Agent 模型配置 (DeepSeek OpenAI 兼容) ──
# 默认使用平台网关 (mcai.mycc.edu.cn/v1) + 网关实际模型 id；
# 运行时 adapter/solver/pi_agent.py 会依据 ANTHROPIC_BASE_URL/ANTHROPIC_MODEL
# 在每个题目 .pi-home 里重新生成该文件，这里只是镜像冷启动的兜底。
RUN mkdir -p /root/.pi/agent && \
    printf '%s\n' \
    '{' \
    '  "providers": {' \
    '    "deepseek": {' \
    '      "baseUrl": "https://mcai.mycc.edu.cn/v1",' \
    '      "api": "openai-completions",' \
    '      "apiKey": "$DEEPSEEK_API_KEY",' \
    '      "models": [' \
    '        {' \
    '          "id": "deepseek-v4-flash-0731",' \
    '          "name": "DeepSeek V4 Flash",' \
    '          "contextWindow": 1000000,' \
    '          "maxTokens": 384000,' \
    '          "input": ["text"],' \
    '          "reasoning": true,' \
    '          "compat": {' \
    '            "requiresReasoningContentOnAssistantMessages": true,' \
    '            "thinkingFormat": "deepseek",' \
    '            "reasoningEffortMap": {' \
    '              "minimal": "high", "low": "high", "medium": "high", "high": "high", "xhigh": "max"' \
    '            }' \
    '          }' \
    '        }' \
    '      ]' \
    '    }' \
    '  }' \
    '}' \
    > /root/.pi/agent/models.json

# ── 4. Python 依赖 ──
COPY requirements.txt /app/requirements.txt
RUN pip3 install --break-system-packages -r /app/requirements.txt

# ── 5. 复制适配器代码 ──
COPY adapter /app/adapter
COPY drivers /app/drivers
COPY skills /app/skills
COPY prompts /app/prompts
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ── 6. 自检 ──
RUN set -eu; mkdir -p /opt/tools; log=/opt/tools/BUILD_SELFCHECK.txt; : > "$log"; missing=""; \
    for b in node python3 curl wget git nmap sqlmap hydra socat ncat pi; do \
        if command -v "$b" >/dev/null 2>&1; then echo "OK   $b" >>"$log"; \
        else echo "MISS $b" >>"$log"; missing="$missing $b"; fi; done; \
    echo "==== SELF-CHECK ====" >>"$log"; \
    if [ -n "$missing" ]; then echo "MISSING:$missing" >>"$log"; cat "$log"; \
        echo "!!! BUILD WARNING: tools missing:$missing"; fi; \
    echo "BUILD OK" >>"$log"; cat "$log"

WORKDIR /app
ENV ADAPTER_WORKDIR=/work IS_SANDBOX=1 TERM=xterm
ENTRYPOINT ["/app/entrypoint.sh"]
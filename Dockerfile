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

# ── 1. 配置 pip 索引（主用 pypi.org；镜像源降级为机会性加速）──
# B35：原配置只写 mirrors.bfsu.edu.cn 一个 index，而该源（以及清华源）在容器内
# 对 pip 返回 403 Forbidden（同一 URL 用 curl 从同容器取是 200，PEP 691 的
# Accept 头从宿主模拟也是 200），pip 于是报 "from versions: none" 直接判构建失败。
# pypi.org 在容器内实测可装。故 index-url 用 pypi.org，国内源放 extra-index-url
# —— 它可用时照常加速，不可用时只是告警，不会再拖垮构建。
RUN printf '[global]\nindex-url = https://pypi.org/simple\nextra-index-url = https://mirrors.bfsu.edu.cn/pypi/web/simple\ntimeout = 120\nretries = 5\n[install]\ntrusted-host = mirrors.bfsu.edu.cn\n' \
        > /etc/pip.conf

# ── 2. 安装 Node.js + Pi Agent（走国内镜像）──
# B34：nodejs.org 对本机限速并会中途停滞（实测 curl 18s 只下到 8.6MB/25.8MB；
# 容器内 wget 卡在 5,737,920 字节 40s 零增长），docker build 会永久挂起 ——
# 镜像因此根本无法重建。npmmirror 同一文件实测 5.0MB/s、全量 5.1s、xz 可解码。
# 上面的 pip 早就配了国内镜像，Node/npm 属同一意图下的遗漏；这里补齐，并给
# wget 加超时(-T)/重试(-t)，即使镜像失效也不会再无限挂起。保留 nodejs.org 兜底。
RUN set -eux; arch="$(uname -m)"; case "$arch" in x86_64) NA=x64;; aarch64|arm64) NA=arm64;; *) NA=x64;; esac; \
    (wget -T 30 -t 2 -q -O /tmp/node.tar.xz "https://npmmirror.com/mirrors/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NA}.tar.xz" \
     || wget -T 30 -t 2 -q -O /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NA}.tar.xz"); \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; rm -f /tmp/node.tar.xz; \
    node --version; \
    npm install -g --ignore-scripts --registry=https://registry.npmmirror.com @earendil-works/pi-coding-agent; \
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
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ── 6. 自检 ──
RUN set -eu; mkdir -p /opt/tools; log=/opt/tools/BUILD_SELFCHECK.txt; : > "$log"; missing=""; \
    for b in node python3 curl wget git nmap sqlmap hydra socat nc pi openvpn ip; do \
        if command -v "$b" >/dev/null 2>&1; then echo "OK   $b" >>"$log"; \
        else echo "MISS $b" >>"$log"; missing="$missing $b"; fi; done; \
    echo "==== SELF-CHECK ====" >>"$log"; \
    if [ -n "$missing" ]; then echo "MISSING:$missing" >>"$log"; cat "$log"; \
        echo "!!! BUILD FAILED: required tools missing:$missing" >&2; exit 1; fi; \
    echo "BUILD OK" >>"$log"; cat "$log"

WORKDIR /app
ENV ADAPTER_WORKDIR=/work IS_SANDBOX=1 TERM=xterm
ENTRYPOINT ["/app/entrypoint.sh"]

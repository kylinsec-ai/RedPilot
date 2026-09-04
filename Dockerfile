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

# ── 3. Pi Agent 模型: 内置 provider 目录,不烤 models.json ──
# 凭据以 pi 官方 env 名提供(DEEPSEEK_API_KEY 等,见 entrypoint/compose/README);
# 自定义/覆盖 provider 时以卷挂载 ~/.pi/agent/models.json(官方格式),勿烤进镜像。
# 内置目录条目数计入下述 BUILD_SELFCHECK(非门禁,超时 5s)。

# ── 4. Python 依赖 ──
COPY requirements.txt /app/requirements.txt
RUN pip3 install --break-system-packages -r /app/requirements.txt

# ── 5. 复制适配器代码 ──
COPY adapter /app/adapter
COPY drivers /app/drivers
COPY web /app/web
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ── 6. 自检 ──
RUN set -eu; mkdir -p /opt/tools; log=/opt/tools/BUILD_SELFCHECK.txt; : > "$log"; missing=""; \
    for b in node python3 curl wget git nmap sqlmap hydra socat ncat pi; do \
        if command -v "$b" >/dev/null 2>&1; then echo "OK   $b" >>"$log"; \
        else echo "MISS $b" >>"$log"; missing="$missing $b"; fi; done; \
    echo "==== SELF-CHECK ====" >>"$log"; \
    echo "pi builtin models: $(timeout 5 pi --list-models 2>/dev/null | wc -l) entries" >>"$log"; \
    if [ -n "$missing" ]; then echo "MISSING:$missing" >>"$log"; cat "$log"; \
        echo "!!! BUILD WARNING: tools missing:$missing"; fi; \
    echo "BUILD OK" >>"$log"; cat "$log"

WORKDIR /app
ENV ADAPTER_WORKDIR=/work IS_SANDBOX=1 TERM=xterm
ENTRYPOINT ["/app/entrypoint.sh"]
# ══════════════════════════════════════════════════════════════
# Ghost 平台接入层适配器 Dockerfile
# 基于已构建的 Ghost Kali 镜像，叠加 Pi Agent + 适配器代码
# ══════════════════════════════════════════════════════════════

FROM ghost/kali:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_VERSION=22.23.2 \
    LANG=C.UTF-8

# ── 1. 配置国内镜像源 (pip) ──
# extra-index-url 兜底 pypi.org:BFSU 对 tsec-benchmark(2026-07 新包)的 simple 索引
# 对 pip 返回空列表(实测 curl 有文件、pip 解析为 none),需官方源补位
RUN printf '[global]\nindex-url = https://mirrors.bfsu.edu.cn/pypi/web/simple\nextra-index-url = https://pypi.org/simple\ntimeout = 120\n[install]\ntrusted-host = mirrors.bfsu.edu.cn\n' \
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

# ── 3.5 题面工具 + 浏览器自动化 ──
# kali-linux-headless 已含 nmap/sqlmap/ffuf/gobuster 等;这里补浏览器与按题面场景
# 的常用工具(S3/对象存储、目录爆破、数据库客户端、crypto 库等)。
# playwright 版本策略:不用 Debian 拆分包(kali-rolling 的 node-playwright 1.38 落后
# python3-playwright 1.55 → driver 握手缺 deviceDescriptors,实测 KeyError),改 pip
# 自包含 wheel(内置 node driver)。chromium 由 apt 提供(playwright CDN 不可达,捆绑
# 浏览器下载必失败),脚本经 executable_path=/usr/bin/chromium 使用(见 tools/ 与下方
# 冒烟;root 运行需 --no-sandbox)。
RUN set -eu; \
    apt-get update; \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        chromium \
        dirsearch feroxbuster nuclei \
        chisel gdb foremost sshpass steghide \
        redis-tools s3cmd awscli \
        python3-pycryptodome python3-sympy python3-gmpy2 python3-z3 python3-pwntools \
        jq qemu-user-static ltrace strace python3-filebytes; \
    # 移除 Debian 拆分版 playwright(版本错配 1.38 driver vs 1.55 py → KeyError),pip 装
    # 自包含 wheel 到 /usr/local(dist-packages 路径无遮蔽);连带移除 node-playwright;
    # theharvester(唯一反向依赖)保留,其 playwright 依赖失效仅在其被调用时报错,本题集不用。
    apt-get -y remove python3-playwright node-playwright; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf /usr/local/bin/node /usr/bin/node; \
    pip3 install --break-system-packages --no-cache-dir "playwright==1.55.0"; \
    # ropper: 用 apt 的 python3-filebytes(依赖),--no-deps 避开源码编译
    pip3 install --break-system-packages --no-cache-dir --no-deps ropper; \
    echo "[build] tools layer done"

# ── 3.6 浏览器冒烟(不 gate 构建,结果进日志) ──
RUN python3 - <<'PY' || echo "[build] WARN: playwright smoke failed (see above)"
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                          args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page()
    pg.goto("data:text/html,<title>pw-ok</title>")
    ok = pg.title() == "pw-ok"
    b.close()
print("[build] playwright smoke:", "OK" if ok else "title mismatch")
assert ok
PY

# ── 4. Python 依赖(三包 monorepo;editable 安装保留 compose 卷热补丁工作流) ──
# --no-build-isolation:镜像 pip 走 BFSU 源,构建隔离会临时拉 setuptools;
# apt 预装 python3-setuptools 后本地构建即可。
# ghost 只装基线(纯 stdlib + contracts):fastapi/pydantic 不进 Kali 镜像(瘦身既定决策)。
# worker 侧只用到 ghost.obs.localserver —— 该模块零 fastapi 依赖,基线安装即可。
RUN apt-get update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        python3-setuptools && rm -rf /var/lib/apt/lists/*
COPY packages/contracts /opt/packages/contracts
COPY packages/ghost /opt/packages/ghost
COPY packages/worker /opt/packages/worker
RUN pip3 install --break-system-packages --no-build-isolation --no-cache-dir \
        -e /opt/packages/contracts -e /opt/packages/ghost -e /opt/packages/worker

# ── 5. 复制题面工具与运行资产 ──
COPY tools /opt/tools
COPY skills /root/.pi/agent/skills
COPY web /app/web
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /opt/tools/*.py

# ── 6. 自检 ──
RUN set -eu; mkdir -p /opt/tools; log=/opt/tools/BUILD_SELFCHECK.txt; : > "$log"; missing=""; \
    for b in node python3 curl wget git nmap sqlmap hydra socat ncat pi \
             chromium dirsearch feroxbuster nuclei aws redis-cli; do \
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
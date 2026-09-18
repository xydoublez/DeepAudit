# syntax=docker/dockerfile:1
# DeepAudit Agent Sandbox
# 安全沙箱环境用于漏洞验证和 PoC 执行
# 集成各类安全扫描工具 (Semgrep, Bandit, Gitleaks, etc.)

FROM docker.m.daocloud.io/python:3.11-bullseye

# apt 镜像源 / pip 索引 / GitHub 下载代理 (可按网络覆盖)
# 例: --build-arg GH_PROXY=https://ghproxy.com/ 或 --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.aliyun.com
ARG GH_PROXY=
# pip 索引: 默认阿里云 (与 apt 同域, 可达性已验证)
# extra-index 清华源: 阿里云缺部分平台 wheel (如 semgrep, 仅同步 sdist, 本地构建的 wheel 不含 semgrep-core 二进制)
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_EXTRA_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

LABEL maintainer="XCodeReviewer Team"
LABEL description="Secure sandbox environment for vulnerability verification and security scanning"

# 安装基本工具
# 使用阿里云镜像加速 apt
# 注意: bullseye LTS 已于 2026-08 到期, 上游删除了 bullseye-security 池中的包文件但索引仍存在,
# 导致镜像站 404; 故移除 security 源, 统一使用主池 (mirrors.aliyun.com/debian) 的版本。
# apt 相关目录 (lists/pkgcache) 均不做缓存挂载: 残留的旧 security 索引/二进制缓存
# 会让 apt 继续选择已删除的 security 版本 (404); 依赖 BuildKit 层缓存即可避免重复下载。
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    sed -i "/bullseye-security/d" /etc/apt/sources.list && \
    sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list && \
    printf 'Package: *\nPin: release n=bullseye\nPin-Priority: 1001\n' > /etc/apt/preferences.d/align-main-archive && \
    apt-get update && apt-get install -y --no-install-recommends --allow-downgrades \
    libc6-dev \
    curl \
    wget \
    netcat-openbsd \
    dnsutils \
    iputils-ping \
    ca-certificates \
    git \
    unzip \
    jq \
    php-cli \
    openjdk-11-jdk-headless \
    ruby-full \
    build-essential \
    cmake \
    clang \
    llvm \
    && rm -f /etc/apt/preferences.d/align-main-archive

# 安装 Node.js (用于 JavaScript/TypeScript 代码执行 和 npm audit)
# 使用淘宝/阿里云镜像加速; nodesource 无国内镜像, 走构建时注入的代理 (docker build 自动传递宿主 proxy 环境变量)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm config set registry https://registry.npmmirror.com \
    && rm -rf /var/lib/apt/lists/*

# 安装 Go (使用阿里云镜像)
ENV PATH=$PATH:/usr/local/go/bin
ENV GOPROXY=https://goproxy.cn,direct
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    curl -L https://mirrors.aliyun.com/golang/go1.21.6.linux-amd64.tar.gz -o go.tar.gz && \
    tar -C /usr/local -xzf go.tar.gz && \
    rm go.tar.gz

# 安装 Rust (使用 rsproxy 镜像)
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH \
    RUSTUP_DIST_SERVER=https://rsproxy.cn \
    RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y --default-toolchain stable --profile minimal && \
    chmod -R a+w /usr/local/cargo

# 安装 Python 安全工具
# semgrep 固定 1.95.0: 更新版本的 wheel 要求 manylinux_2_34 (glibc>=2.34),
# bullseye 为 glibc 2.31, pip 会回落 sdist 源码构建 (产物缺 semgrep-core 二进制, 无法运行)
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
    pip install -i ${PIP_INDEX} --extra-index-url ${PIP_EXTRA_INDEX} \
    requests \
    httpx \
    aiohttp \
    beautifulsoup4 \
    lxml \
    pycryptodome \
    paramiko \
    pyjwt \
    python-jose \
    sqlparse \
    semgrep==1.95.0 \
    bandit \
    safety

# 构建期代理 (GitHub 等境外下载用): docker build 不会自动注入宿主环境,
# 需构建命令显式 --build-arg HTTPS_PROXY=... 传入 (国内源步骤均已 unset, 不受影响)
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
# 安装 Gitleaks (GitHub 直连不可达, 走构建时注入的代理)
RUN \
    curl -L ${GH_PROXY}https://github.com/gitleaks/gitleaks/releases/download/v8.18.2/gitleaks_8.18.2_linux_x64.tar.gz -o gitleaks.tar.gz && \
    tar -xzf gitleaks.tar.gz && \
    mv gitleaks /usr/local/bin/ && \
    rm gitleaks.tar.gz

# 安装 TruffleHog (GitHub 直连不可达, 走构建时注入的代理)
RUN \
    curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin

# 安装 OSV-Scanner (GitHub 直连不可达, 走构建时注入的代理)
RUN \
    curl -L ${GH_PROXY}https://github.com/google/osv-scanner/releases/download/v1.6.2/osv-scanner_1.6.2_linux_amd64 -o /usr/local/bin/osv-scanner && \
    chmod +x /usr/local/bin/osv-scanner

# 创建非 root 用户
RUN groupadd -g 1000 sandbox && \
    useradd -u 1000 -g sandbox -m -s /bin/bash sandbox

# 创建工作目录
RUN mkdir -p /workspace /tmp/sandbox && \
    chown -R sandbox:sandbox /workspace /tmp/sandbox

# 设置环境变量
ENV HOME=/home/sandbox
ENV PATH=/home/sandbox/.local/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 限制 Python 导入路径
ENV PYTHONPATH=/workspace

# 切换到非 root 用户
USER sandbox

WORKDIR /workspace

# 默认命令
CMD ["/bin/bash"]


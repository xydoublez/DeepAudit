# syntax=docker/dockerfile:1
# ============================================
# 多阶段构建 - 构建阶段
# ============================================
FROM python:3.12-slim AS builder

# apt 镜像源 (可按网络覆盖, 例: --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn)
ARG APT_MIRROR=mirrors.aliyun.com
# PyPI 索引 (uv 下载依赖; 国内可覆盖为 https://pypi.tuna.tsinghua.edu.cn/simple)
ARG UV_INDEX=https://pypi.org/simple/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 彻底清除代理设置
ENV http_proxy=""
ENV https_proxy=""
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""
ENV all_proxy=""
ENV ALL_PROXY=""
ENV no_proxy="*"
ENV NO_PROXY="*"

# 清除 apt 代理配置并配置重试，使用阿里云镜像
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true && \
    rm -f /etc/apt/apt.conf.d/proxy.conf 2>/dev/null || true && \
    echo 'Acquire::http::Proxy "false";' > /etc/apt/apt.conf.d/99-no-proxy && \
    echo 'Acquire::https::Proxy "false";' >> /etc/apt/apt.conf.d/99-no-proxy && \
    echo 'Acquire::Retries "5";' >> /etc/apt/apt.conf.d/99-no-proxy && \
    echo 'Acquire::http::Timeout "60";' >> /etc/apt/apt.conf.d/99-no-proxy

# 安装构建依赖（gcc 只在构建阶段需要）
# BuildKit 缓存挂载: 重复构建复用 apt 缓存, 不再每次重新下载 (原该步约 516s)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        libffi-dev

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 配置 uv 索引 (默认官方 PyPI; 国内可 --build-arg UV_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple)
ENV UV_INDEX_URL=${UV_INDEX}

# 复制依赖文件
COPY pyproject.toml uv.lock README.md ./

# 安装 Python 依赖到虚拟环境 (uv 缓存挂载加速重复构建)
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev

# ============================================
# 多阶段构建 - 运行阶段
# ============================================
FROM python:3.12-slim AS runtime

ARG APT_MIRROR=mirrors.aliyun.com

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 彻底清除代理设置
ENV http_proxy=""
ENV https_proxy=""
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""
ENV all_proxy=""
ENV ALL_PROXY=""
ENV no_proxy="*"
ENV NO_PROXY="*"

# 配置使用阿里云 Debian 镜像源
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true

# 清除 apt 代理配置
RUN rm -f /etc/apt/apt.conf.d/proxy.conf 2>/dev/null || true && \
    echo 'Acquire::http::Proxy "false";' > /etc/apt/apt.conf.d/99-no-proxy && \
    echo 'Acquire::https::Proxy "false";' >> /etc/apt/apt.conf.d/99-no-proxy && \
    echo 'Acquire::Retries "5";' >> /etc/apt/apt.conf.d/99-no-proxy && \
    echo 'Acquire::http::Timeout "60";' >> /etc/apt/apt.conf.d/99-no-proxy

# 只安装运行时依赖（不需要 gcc）
# BuildKit 缓存挂载: 重复构建复用 apt 缓存 (原该步约 516s; fonts-noto-cjk 体积大, 缓存收益显著)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        git \
        openssh-client \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libpangocairo-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libglib2.0-0 \
        shared-mime-info \
        fonts-noto-cjk \
    && fc-cache -fv

# 从构建阶段复制虚拟环境和 uv
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

# 复制应用代码
COPY . .

# 创建上传目录并设置启动脚本权限
RUN mkdir -p /app/uploads/zip_files && \
    chmod +x /app/docker-entrypoint.sh

# 暴露端口
EXPOSE 8000

# 启动命令 - 使用启动脚本自动执行数据库迁移
CMD ["/app/docker-entrypoint.sh"]

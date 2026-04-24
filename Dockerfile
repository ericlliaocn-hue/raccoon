# Raccoon AI Agent Framework
# Multi-stage build: builder → runtime

# ── Builder Stage ──────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 安装系统依赖（编译用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# 安装 Playwright 浏览器（headless 模式用）
RUN playwright install chromium --with-deps

# ── Runtime Stage ──────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 安装运行时系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright 依赖
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    # Chrome 依赖（CDP 模式）
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制 Python 包
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制 Playwright 浏览器
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

# 复制项目文件
COPY . .

# 创建数据目录
RUN mkdir -p /app/data /app/output /app/skills /app/workflows

# 环境变量
ENV RACCOON_HTTP_HOST=0.0.0.0
ENV RACCOON_HTTP_PORT=8900
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8900/health')" || exit 1

# 暴露端口
EXPOSE 8900

# 启动命令
CMD ["python", "raccoon.py", "--http", "--host", "0.0.0.0", "--port", "8900"]
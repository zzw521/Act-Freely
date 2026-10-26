# syntax=docker/dockerfile:1
#
# 通天办公 · 引擎 + 静态前端容器镜像（模板）
#
# 重要：本文件未在本仓库开发机验证（开发机没有 Docker）。构建/运行前请先阅读
# deploy/README.md 的「验证状态」。验证过的部分是引擎自身的静态托管、令牌守卫与
# 非回环绑定准入；容器层（基础镜像、依赖安装、卷权限）需要在本机之外单独验收。
#
# 构建（在 tongtian-tauri 目录执行）：
#   docker build -f deploy/Dockerfile -t tongtian-office:dev .
# 运行（必须提供令牌，否则引擎会拒绝绑定 0.0.0.0 并退出）：
#   docker run --rm -p 18000:18000 -e TT_ENGINE_TOKEN=<令牌> -v tt-data:/data tongtian-office:dev

# ── 前端构建：VITE_MOCK=false 才会进入引擎直连通道 ──
FROM node:22-slim AS web
WORKDIR /app
COPY package.json package-lock.json vite.config.ts postcss.config.js tailwind.config.js ./
RUN npm ci
COPY src ./src
ENV VITE_MOCK=false
RUN npm run build

# ── 运行时：Python 引擎 + 构建产物 ──
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY engine/requirements.txt ./engine/requirements.txt
RUN pip install --no-cache-dir -r engine/requirements.txt

COPY engine ./engine
COPY --from=web /app/dist ./dist

# 非 root 运行；/data 存放 models.json、意识、IM 凭据、检查点与工作目录
RUN useradd --create-home --uid 10001 tongtian \
    && mkdir -p /data/work \
    && chown -R tongtian:tongtian /app /data

ENV TT_WEB_DIR=/app/dist \
    TONGTIANWORK_HOME=/data \
    TT_DATA_HOME=/data \
    TT_ALLOW_REMOTE_BIND=1

USER tongtian
EXPOSE 18000

HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18000/health', timeout=2)" || exit 1

CMD ["python", "engine/server.py", "--host", "0.0.0.0", "--port", "18000", "--work-dir", "/data/work", "--web-dir", "/app/dist"]

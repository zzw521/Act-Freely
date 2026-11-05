# 生产镜像：单副本、单 worker、SQLite；缺生产必填项时应用会拒绝启动（见 docs/deploy.md）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY schemas ./schemas

EXPOSE 8001

# 默认单 worker；TT_GATEWAY_WORKERS>1 时必须同时配置 TT_GATEWAY_REDIS_URL（启动自检会拒绝）
# --no-proxy-headers：保留真实对端地址，管理面 mTLS 的受信代理判断不能被 X-Forwarded-For 绕过
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers \"${TT_GATEWAY_WORKERS:-1}\" --no-proxy-headers"]

# 通天办公服务端：FastAPI 单进程镜像。
# 授权码与限流器在进程内存中，必须固定 --workers 1，不能横向扩容。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TT_SERVER_HOST=0.0.0.0 \
    TT_SERVER_PORT=8000 \
    TT_SERVER_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker ./docker

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${TT_SERVER_HOST:-0.0.0.0}\" --port \"${TT_SERVER_PORT:-8000}\" --workers 1"]

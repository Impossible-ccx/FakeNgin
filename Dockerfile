# FakeNgin 谣言检测平台生产部署镜像
# 数据（SQLite/索引）默认写入 /app/database，部署时挂载卷持久化
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 先装依赖，充分利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY web/ web/
COPY scripts/ scripts/
COPY samples/ samples/

# 非 root 运行；UID 1000 与常见宿主机首用户一致，便于 bind mount 读写
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /app/database \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=4)" || exit 1

# 多 worker 下检测队列按“原子认领”安全并发；FLASK_SECRET_KEY 必须由环境固定
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", \
     "--timeout", "180", "--access-logfile", "-", "app:app"]

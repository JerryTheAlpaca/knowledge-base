FROM python:3.12-slim

WORKDIR /app

# 锁定依赖安装；运行时不自动更新包（docs/02 §15.1）
# 腾讯云服务器访问官方 PyPI 不稳，固定走清华镜像（仅构建期）
COPY pyproject.toml ./
COPY kbserver ./kbserver
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple fastapi "uvicorn[standard]" sqlalchemy alembic pydantic cryptography python-multipart httpx

# FFmpeg：ASR 音频准备（docs/11 §5.2）经管道解码用；固定随镜像分发，不运行时安装
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行；预创建数据卷挂载点，避免 Docker 以 root 重建挂载点导致非 root 容器无权写入
RUN useradd --create-home appuser && mkdir -p /data/db /data/objects /data/tmp /data/deletions \
    && chown -R appuser /data /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "kbserver.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

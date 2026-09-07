FROM python:3.12-slim

WORKDIR /app

# 锁定依赖安装；运行时不自动更新包（docs/02 §15.1）
COPY pyproject.toml ./
COPY kbserver ./kbserver
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" sqlalchemy alembic pydantic cryptography python-multipart httpx

# 非 root 运行
RUN useradd --create-home appuser && mkdir -p /data && chown -R appuser /data /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "kbserver.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

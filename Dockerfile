FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 安装 uv 包管理器（多阶段复制，避免残留）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 先拷贝依赖清单，利用构建缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# 拷贝项目源码与默认资源
COPY . .

EXPOSE 8000

# 健康检查：用于 deploy 脚本 / docker 判断容器是否正常运行
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

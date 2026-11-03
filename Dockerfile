# syntax=docker/dockerfile:1.7
#
# web_agent 主服务的镜像。
#
# 与 studio 镜像（``services/studio/Dockerfile``）是**两个独立镜像**：
# studio 只认「一段脚本 + 一组参数」，不认识 web_agent；web_agent 也不该装
# grpcio 之外的 studio 运行时。
#
# 三条不能破的原则：
#
# 1. **不把任何凭据打进镜像**。``.env`` 走挂载或 compose 的 env_file，
#    不 COPY 进去——镜像层是公开的，`docker history` 一看就知道。
# 2. **版本钉死**：基础镜像用具体版本号，不用 ``latest``。
# 3. **非 root**：uid 1000，与 studio 一致。

FROM python:3.11.9-slim-bookworm

# PIP_INDEX_URL 做成构建参数：默认走官方源，国内构建时用
# `--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` 换镜像。
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：改业务代码时这一层能命中缓存，不用重装一遍。
COPY requirements.txt ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

# 只拷运行时真正需要的东西。测试、文档、本地开发脚本都不进镜像。
COPY alembic.ini run.py ./
COPY .env.example ./
COPY alembic/ ./alembic/
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY web/ ./web/
COPY proto/ ./proto/
COPY webagent/ ./webagent/

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && chown -R 1000:1000 /app
USER 1000

EXPOSE 8000

# 健康检查用 python 自带的 urllib，不额外装 curl（镜像小一点）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

# run.py 会先跑 alembic upgrade head 再起 uvicorn。
# --no-seed 不灌演示数据（演示数据是本地开发用的，不是生产数据）。
# --no-reload 不开热重载：容器里跑重载进程等于跑两个 uvicorn。
CMD ["python", "run.py", "--no-seed", "--no-reload", "--host", "0.0.0.0", "--port", "8000"]

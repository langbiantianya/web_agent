# syntax=docker/dockerfile:1.7
#
# studio-dispatcher：只调度、不执行的沙箱控制面（skill-service §2.4）。
#
# 它持有 Docker socket，但**从不执行 content**；真正跑脚本的是一次性沙箱容器
# （services/sandbox/Dockerfile）。所以这个镜像可以装 docker SDK——沙箱镜像不行。
#
# 安全边界：dispatcher 的凭据是「能创建容器」。它只按固定模板创建：镜像来自目录、
# 无网络、只读 rootfs、非 root、capabilities 全 drop。调用方不能传容器模板。
#
# **lab 里以 root 跑**是为了挂 /var/run/docker.sock（socket 通常是 root:docker）。
# 生产建议用 docker-socket-proxy 把 API 收敛成只允许 create/start/logs/remove，
# 再让 dispatcher 以非 root + 代理地址运行——这一步不在 Phase 1。

FROM python:3.11.9-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY services/dispatcher/requirements.txt ./requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY services/__init__.py ./services/__init__.py
COPY services/sandbox/ ./services/sandbox/
COPY services/dispatcher/ ./services/dispatcher/
COPY services/sandbox/seccomp-profile.json /etc/webagent/seccomp-profile.json

EXPOSE 9100

CMD ["python", "-m", "services.dispatcher"]

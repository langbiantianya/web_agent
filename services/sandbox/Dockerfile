# syntax=docker/dockerfile:1.7
#
# 一次性沙箱镜像（skill-service §2.4、§10.1）。
#
# 这个镜像里**只有**三样运行时 + sandbox runner，没有 studio 服务、没有
# 数据库驱动、没有任何凭据。它由 dispatcher 以「一次性容器」的方式启动：
#
#   --network none --read-only --user 1000 --cap-drop ALL
#   --security-opt no-new-privileges --pids-limit 256 --memory 512m
#
# 脚本的输入是 /run/sandbox/payload.json（dispatcher 在容器启动前写入），
# 输出是容器 stdout 上的控制帧（见 services/sandbox/protocol.py）。

FROM node:20.18.1-bookworm-slim AS node

FROM python:3.11.9-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

ARG APT_MIRROR=
RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        libstdc++6 \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY services/__init__.py ./services/__init__.py
COPY services/sandbox/ ./services/sandbox/

# 非 root（§7）。/run/sandbox 是 dispatcher 在启动前写 payload 的位置；
# 容器以 read-only rootfs 启动，运行期它不可写。
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /run/sandbox \
    && chown -R 1000:1000 /app /run/sandbox

USER 1000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "services.sandbox.runner"]

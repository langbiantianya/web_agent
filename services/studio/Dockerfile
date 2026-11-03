# syntax=docker/dockerfile:1.7
#
# studio 的镜像（skill-service §7、§10.1、§13）。
#
# 三条不能破的原则：
#
# 1. **零凭据**——不注入任何 env、不挂任何 secret。它执行的是模型指定的代码，
#    容器里只要有一个能用的 key，一次提示词注入就够把 key 偷出去。
# 2. **只读 rootfs + 非 root**——脚本只往 cwd（临时目录）写。
# 3. **运行时固定版本**——python / bash / node 三样，版本钉死。
#
# 这几条在**编排平台的容器配置**里也要再设一遍（只读 rootfs、drop capabilities、
# no-new-privileges、seccomp、pidsLimit、禁出网）。镜像里能做的是「非 root」与
# 「不装多余的东西」，其余靠 K8s 的 securityContext 与 NetworkPolicy。

# ---------------------------------------------------------------- 运行时来源
#
# node 不在 python 镜像里，单独从一个 node 官方镜像拷二进制过来（多阶段构建）。
FROM node:20.18.1-bookworm-slim AS node

FROM python:3.11.9-slim-bookworm

ARG STUDIO_VERSION=1.0.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STUDIO_VERSION=${STUDIO_VERSION} \
    STUDIO_HOST=0.0.0.0 \
    STUDIO_PORT=9000

# bash 是三种运行时之一（§10.1）；libstdc++6 是 node 二进制的动态依赖。
# tini 是 init 进程——见下面的 ENTRYPOINT。
#
# APT_MIRROR 做成构建参数：默认走官方源，国内构建时用
# `--build-arg APT_MIRROR=mirrors.aliyun.com` 换镜像。官方源从国内拉经常
# 中途断流（apt 会报 "Ign" 然后重试，一个 Packages 文件能卡十几分钟）。
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

# ---------------------------------------------------------------- 依赖与代码
WORKDIR /app

COPY services/studio/requirements.txt ./requirements.txt
# PIP_INDEX_URL 做成构建参数：默认走官方源，国内构建时用
# `--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` 换镜像，
# 不用为了换源改镜像本身。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

# 只拷需要的三样：生成的桩代码、契约文件、服务端代码。
# **不拷 web_agent 的代码**——studio 不认识 web_agent（§2.3）。
COPY proto/ ./proto/
COPY webagent/ ./webagent/
COPY services/ ./services/

# 非 root（§7）。uid 1000 是 K8s 里常见的约定；K8s 的 securityContext 里还要
# 再写一遍 runAsNonRoot / runAsUser，两边一致才不会出现「镜像里是 root 但
# K8s 说非 root」的错配。
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin studio \
    && chown -R 1000:1000 /app
USER 1000

EXPOSE 9000

# 健康检查探针用（§13.3）。K8s 的 grpc: 探针发的是 gRPC 健康检查标准协议
# grpc.health.v1.Health/Check，跟我们的 Health 方法对不上，所以用 exec 探针
# 调我们自己的方法。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "services.studio.healthcheck", "--addr", "127.0.0.1:9000"]

# **必须用 init 进程（tini）当 PID 1**，不能直接让 python 当 PID 1。
#
# 原因：脚本被超时杀掉时，它自己起的子进程（孙进程）会被**重新挂到 PID 1**
# 也就是 studio 服务底下。python 不是 init、不会回收孤儿，被杀死的孙进程
# 会变成**僵尸**（/proc/<pid> 还在、状态 Z）——PID 表被慢慢占满，容器跑久了
# 就「pid 耗尽」起不来新进程了。
#
# 实测（真容器）：一次超时 + 脚本带子进程 = 容器里留下一个僵尸。tini 会
# 回收它，顺带把 SIGTERM 正确转发给 python（优雅停机那条路也就通了）。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "services.studio"]

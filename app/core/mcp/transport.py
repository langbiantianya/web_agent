"""MCP 传输层（设计文档 §21.4、§21.10 第 ② 条）。

两种传输：

- **stdio**：本地子进程。``command`` / ``args`` 来自配置——**这是全设计里唯一一处
  「配置决定执行什么代码」**（§21.10 第 ① 条），所以命令路径必须在白名单前缀内；
- **Streamable HTTP**：远程端点。URL 来自配置，于是是 **SSRF 面**——出网地址要
  过白名单与私网网段检查。

旧的 HTTP+SSE 双端点传输**不实现**（2025-03-26 废弃）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from app.config.settings import settings

logger = logging.getLogger(__name__)


class McpTransportError(Exception):
    """传输层拒绝或失败——配置非法、地址被策略拦下、连不上。"""


class McpStderrWriter:
    """把 MCP 子进程的 stderr 收进日志。

    **stdout 只能有协议消息**——stdio 模式下服务端随手一句 ``print`` 就会把
    JSON-RPC 帧流弄乱。所以子进程的 stdout 必须是干净的（SDK 已经保证：它自己
    只读 stdout 的协议帧），而普通日志走 stderr，这里把它接到 logger 上，
    不让它直接落到服务进程的 stderr 上。

    ## 为什么需要一根真管道

    SDK 把 ``errlog`` 直接当 ``subprocess`` 的 ``stderr=`` 用，而 ``subprocess``
    要的是**真正的文件描述符**（``stderr.fileno()``），不是「一个能写字符串的
    对象」。所以这里开一根 OS 管道：写端交给子进程，读端由一个后台线程读出来、
    按行记进 logger。

    这样子进程的输出不会落到服务进程的 stderr 上，而是变成带服务器 code 的
    结构化日志——出问题时能一眼看出是哪台服务器在抱怨。

    Attributes:
        server_code: 哪台服务器的 code，日志里用它标注来源。
    """

    def __init__(self, server_code: str) -> None:
        """开管道并起后台读取线程。

        Args:
            server_code: 哪台服务器的日志。
        """
        self.server_code = server_code
        self._read_fd, self._write_fd = os.pipe()
        self._closed = False
        self._thread = threading.Thread(
            target=self._pump,
            name=f"mcp-stderr-{server_code}",
            daemon=True,
        )
        self._thread.start()

    def fileno(self) -> int:
        """给子进程用的写端 fd。

        ``subprocess`` 只认 fd——这是整件事能成立的前提。

        Returns:
            管道写端的文件描述符。
        """
        return self._write_fd

    def _pump(self) -> None:
        """后台线程：把子进程的 stderr 读出来按行记进 logger。"""
        try:
            with os.fdopen(self._read_fd, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    stripped = line.rstrip()
                    if stripped:
                        logger.info("MCP %s stderr: %s", self.server_code, stripped)
        except (OSError, ValueError):  # pragma: no cover - 管道被拆了
            logger.debug("MCP %s stderr 读取线程结束", self.server_code, exc_info=True)

    def write(self, text: str) -> int:
        """写一段文本。

        实际不走这里——子进程直接写 fd。这个方法只是为了满足 TextIO 协议。

        Args:
            text: 要写的文本。

        Returns:
            写入的字符数。
        """
        return len(text or "")

    def flush(self) -> None:
        """TextIO 协议要求有这个方法——没有它 SDK 会报错。"""

    def isatty(self) -> bool:
        """TextIO 协议要求有这个方法。管道不是终端。"""
        return False

    def close(self) -> None:
        """关掉写端并等读取线程收尾。

        关掉写端后子进程再写就会收到 EPIPE，读取线程读到 EOF 自然退出。
        """
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._write_fd)
        except OSError:  # pragma: no cover - 已被关闭
            pass
        self._thread.join(timeout=2)


# --------------------------------------------------------------- SSRF 防护


def _is_forbidden_address(addr: str) -> bool:
    """这个 IP 是不是「内网 / 回环 / 链路本地 / 保留」地址。

    Args:
        addr: IP 地址字符串。

    Returns:
        该地址必须被拒绝时返回 True。
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # 不是合法 IP（DNS 给了个怪东西）——按不可信处理。
        return True
    # IPv4 映射的 IPv6（::ffff:10.0.0.1）要按 IPv4 判。
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve(host: str) -> list[str]:
    """把主机名解析成 IP 列表。

    Args:
        host: 主机名或 IP。

    Returns:
        解析出的 IP 字符串列表。

    Raises:
        McpTransportError: 解析失败。
    """
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise McpTransportError(f"解析主机名失败：{host}（{exc}）") from exc
    return [info[4][0] for info in infos]


async def check_url_allowed(url: str, *, require_dns: bool = True) -> None:
    """按出网策略检查一个 Streamable HTTP 端点。

    三道：

    1. **协议白名单**：只允许 http / https；
    2. **主机白名单**（``MCP_ALLOWED_HOSTS``）：非空时只有名单里的主机能连；
    3. **私网网段默认拒绝**：``10/8`` ``172.16/12`` ``192.168/16`` ``127/8``
       ``169.254/16`` ``::1`` ``fc00::/7`` 一律拒绝，除非显式放行。

    重定向：**跨源重定向不会跟随**——SDK 本身就把重定向限制在同源内，所以
    「重定向到私网」这条路在传输层就断了，不需要在这里再跟一次。

    ## 保存期与连接期为什么分开

    ``require_dns=False``（**保存期**）时，**解析失败不拦**；``require_dns=True``
    （**连接期**）时，解析失败一律拒。

    理由：DNS 解析失败说明的是「这个主机此刻查不到」，不是「这个地址危险」。
    而「此刻查不到」在实际部署里很常见——服务还没上线、内外网分裂 DNS
    （split-horizon）、DNS 抖动。拿它去卡保存期，会把**恰好最该用的那类场景**
    （内网 MCP 服务器）挡在门外。

    危险判定仍然两道都不松：
    - **IP 字面量**（``http://127.0.0.1:8931/mcp``）不需要 DNS 就能判定——那是
      最常见的 SSRF 载荷，保存期照样拒；
    - 真正连上去之前会**再查一次**（``open_transport`` 用的是
      ``require_dns=True``），那时解析结果如何都拦得住。

    Args:
        url: 端点 URL。
        require_dns: 解析失败时是否拒绝。保存期传 False，连接期传 True。

    Raises:
        McpTransportError: 被策略拒绝。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise McpTransportError(f"只允许 http / https 端点：{url}")
    host = parsed.hostname
    if not host:
        raise McpTransportError(f"端点缺少主机名：{url}")

    allowed_hosts = [h.strip().lower() for h in settings.mcp_allowed_hosts if h.strip()]
    if allowed_hosts and host.lower() not in allowed_hosts:
        raise McpTransportError(
            f"主机「{host}」不在出网白名单里（配置 MCP_ALLOWED_HOSTS 后生效）"
        )

    if settings.mcp_allow_private_networks:
        return

    try:
        addrs = await _resolve(host)
    except McpTransportError:
        if require_dns:
            raise
        # 保存期：查不到就放行，但留一条日志。连接期会再查一次，那时不容情。
        logger.warning(
            "MCP 端点 %s 的主机 %s 现在解析不了，保存期先放行；连接时仍会校验",
            url,
            host,
        )
        return

    for addr in addrs:
        if _is_forbidden_address(addr):
            raise McpTransportError(
                f"端点解析到内网/保留地址（{addr}），按 SSRF 策略拒绝：{host}"
            )


def _normalize_path(value: str) -> str:
    """把路径统一成「正斜杠 + 无末尾分隔符」的形状。

    Windows 上路径是 ``C:\\...\\python.exe``，POSIX 上是 ``/usr/bin/python``。
    不统一就没法用同一套前缀逻辑——用 ``startswith(prefix + "/")`` 去比
    Windows 路径，永远不成立。
    """
    return value.replace("\\", "/").rstrip("/")


def check_command_allowed(command: str) -> None:
    """按命令白名单检查一个 stdio 启动命令。

    ``MCP_ALLOWED_COMMAND_PREFIXES`` **默认为空 = 禁止 stdio 注册**（fail-closed）：
    注册一台 stdio 服务器就是「允许执行某个程序」，这道门默认开着等于没有门。

    前缀匹配按**目录**算：前缀 ``/opt/tools`` 命中 ``/opt/tools/run``，但不命中
    ``/opt/tools-evil/run``——后者只是名字长得像，不在那个目录里。

    Args:
        command: 启动命令（可执行文件路径）。

    Raises:
        McpTransportError: 命令不在白名单前缀内。
    """
    prefixes = [p.strip() for p in settings.mcp_allowed_command_prefixes if p.strip()]
    if not prefixes:
        raise McpTransportError(
            "未配置 MCP_ALLOWED_COMMAND_PREFIXES，stdio 类型的 MCP 服务器不允许注册"
            "（这是有意的：注册 stdio 服务器等于允许执行某个程序）"
        )
    norm_command = _normalize_path(command)
    for prefix in prefixes:
        norm_prefix = _normalize_path(prefix)
        if not norm_prefix:
            continue
        if norm_command == norm_prefix or norm_command.startswith(norm_prefix + "/"):
            return
    raise McpTransportError(
        f"命令「{command}」不在白名单前缀内：{'、'.join(prefixes)}"
    )


# --------------------------------------------------------------- 连接建立


@asynccontextmanager
async def open_transport(server) -> AsyncIterator[tuple[Any, Any]]:
    """建立一条到 MCP 服务器的连接，产出 ``(读流, 写流)``。

    Args:
        server: ORM ``McpServer`` 实例（``env`` / ``headers`` 里是**密文**）。

    Yields:
        ``(read_stream, write_stream)``，交给 ``ClientSession`` 用。

    Raises:
        McpTransportError: 配置非法或地址被策略拦下。
    """
    from app.core.crypto import decrypt

    if server.transport == "stdio":
        if not server.command:
            raise McpTransportError(f"MCP 服务器「{server.code}」没有配置启动命令")
        check_command_allowed(server.command)
        env = {
            key: decrypt(value) for key, value in (server.env or {}).items()
        }
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=server.command,
            args=list(server.args or []),
            env=env or None,
        )
        errlog = McpStderrWriter(server.code)
        try:
            async with stdio_client(params, errlog=errlog) as streams:
                yield streams
        finally:
            # 管道与后台线程都要收掉——不收的话每次重连都漏一根管道和一个线程。
            errlog.close()

    elif server.transport == "http":
        if not server.url:
            raise McpTransportError(f"MCP 服务器「{server.code}」没有配置端点 URL")
        await check_url_allowed(server.url)
        headers = {
            key: decrypt(value) for key, value in (server.headers or {}).items()
        }
        # httpx2 是 MCP SDK 2.x 用的 httpx 分支（与本项目自己用的 httpx 1.x
        # 并存）。延迟导入，避免把一个 SDK 内部依赖写进模块顶层。
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        timeout = httpx2.Timeout(float(server.timeout_sec or 30))
        async with httpx2.AsyncClient(
            headers=headers or None, timeout=timeout, follow_redirects=False
        ) as client:
            async with streamable_http_client(server.url, http_client=client) as streams:
                yield streams

    else:
        raise McpTransportError(
            f"不支持的传输方式：{server.transport!r}（只支持 stdio / http）"
        )

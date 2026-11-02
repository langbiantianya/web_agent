"""MCP 传输层的安全边界（设计文档 §21.10 第 ① ② 条）。

两条边界各自对应一种「配置决定做什么」的风险：

- **stdio**：``command`` / ``args`` 决定执行**哪个程序**——全设计里唯一一处
  「配置决定执行什么代码」的地方，所以命令必须落在白名单前缀内，且
  **白名单默认为空 = 禁止注册**（fail-closed）。
- **Streamable HTTP**：``url`` 决定往**哪里**发请求——这是 SSRF 面。协议白名单、
  主机白名单、私网网段三道一起看。

这两道门都必须在**保存期**生效（API 层已经接上了），这里钉的是传输层本身的行为。
"""

from __future__ import annotations

import pytest

from app.config.settings import settings
from app.core.mcp import transport as transport_mod
from app.core.mcp.transport import (
    McpTransportError,
    check_command_allowed,
    check_url_allowed,
)


# --------------------------------------------------------------- stdio 命令白名单


def test_stdio_is_denied_when_no_prefix_configured(monkeypatch):
    """白名单为空 = 禁止 stdio。

    注册一台 stdio 服务器等于「允许执行某个程序」。默认把门开着等于没有门——
    这道门必须由部署方**显式**打开。
    """
    monkeypatch.setattr(settings, "mcp_allowed_command_prefixes", [])
    with pytest.raises(McpTransportError) as ei:
        check_command_allowed("/usr/bin/python")
    assert "MCP_ALLOWED_COMMAND_PREFIXES" in str(ei.value)


def test_stdio_accepts_command_inside_prefix(monkeypatch):
    """前缀内的命令放行。"""
    monkeypatch.setattr(settings, "mcp_allowed_command_prefixes", ["/usr/bin/"])
    check_command_allowed("/usr/bin/python")
    check_command_allowed("/usr/bin/python3")


def test_stdio_rejects_command_outside_prefix(monkeypatch):
    """前缀外的命令拒掉。

    ``/usr/bin`` 与 ``/usr/bin/evil`` 是两个不同的前缀匹配结果——白名单要么精确
    等于，要么以分隔符结尾才算命中，否则 ``/usr/local/bin/evil`` 会被
    ``/usr/local`` 意外放行。
    """
    monkeypatch.setattr(settings, "mcp_allowed_command_prefixes", ["/usr/bin/"])
    with pytest.raises(McpTransportError) as ei:
        check_command_allowed("/bin/sh")
    assert "白名单" in str(ei.value)


def test_stdio_rejects_prefix_lookalike(monkeypatch):
    """相似前缀不得被当成命中。"""
    monkeypatch.setattr(settings, "mcp_allowed_command_prefixes", ["/opt/tools"])
    with pytest.raises(McpTransportError):
        check_command_allowed("/opt/tools-evil/run")


# --------------------------------------------------------------- SSRF 防护


async def test_url_rejects_non_http_scheme():
    """只允许 http / https。

    ``file://`` / ``gopher://`` 之类能读本地文件或打到别的协议上。
    """
    for url in ("file:///etc/passwd", "gopher://127.0.0.1:70/x", "ftp://h/x"):
        with pytest.raises(McpTransportError) as ei:
            await check_url_allowed(url)
        assert "http" in str(ei.value)


async def test_url_requires_hostname():
    """没有主机名的 URL 拒掉。"""
    with pytest.raises(McpTransportError):
        await check_url_allowed("http:///mcp")


async def test_url_rejects_private_network_by_default(monkeypatch):
    """私网 / 回环 / 链路本地地址默认拒绝。

    这条是 SSRF 的核心：URL 来自配置，而配置能被管理员之外的人看到（列表接口
    不需要管理员密钥）。不挡住内网，一个端点就能把内网服务扫一遍。
    """
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254"):
        with pytest.raises(McpTransportError) as ei:
            await check_url_allowed(f"http://{host}/mcp")
        assert "SSRF" in str(ei.value)


async def test_url_rejects_ipv6_loopback(monkeypatch):
    """IPv6 回环同样拒。"""
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    with pytest.raises(McpTransportError):
        await check_url_allowed("http://[::1]/mcp")


async def test_url_allows_private_when_explicitly_allowed(monkeypatch):
    """显式放行时私网地址可以通过。

    本机开发常常就要连 ``127.0.0.1`` 上跑着的 MCP 服务器——但那必须是**显式**
    的开关，不是默认。
    """
    monkeypatch.setattr(settings, "mcp_allow_private_networks", True)
    await check_url_allowed("http://127.0.0.1:8931/mcp")


async def test_url_enforces_host_allowlist(monkeypatch):
    """主机白名单非空时，只有名单里的主机能连。"""
    monkeypatch.setattr(settings, "mcp_allow_private_networks", True)
    monkeypatch.setattr(settings, "mcp_allowed_hosts", ["mcp.example.com"])
    await check_url_allowed("https://mcp.example.com/mcp")
    with pytest.raises(McpTransportError) as ei:
        await check_url_allowed("https://evil.example.com/mcp")
    assert "出网白名单" in str(ei.value)


async def test_url_resolves_before_connecting(monkeypatch):
    """DNS 解析到内网地址时要拒绝。

    这是「看起来是公网域名、实际解析到内网」那条绕过路径——只检查字面 IP 不够。
    """
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    monkeypatch.setattr(settings, "mcp_allowed_hosts", [])

    async def fake_resolve(_host: str) -> list[str]:
        """假装解析到内网地址。"""
        return ["169.254.169.254"]

    monkeypatch.setattr(transport_mod, "_resolve", fake_resolve)
    with pytest.raises(McpTransportError) as ei:
        await check_url_allowed("https://looks-public.example.com/mcp")
    assert "SSRF" in str(ei.value)


async def test_url_allows_public_address(monkeypatch):
    """解析到公网地址时放行。"""
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    monkeypatch.setattr(settings, "mcp_allowed_hosts", [])

    async def fake_resolve(_host: str) -> list[str]:
        """假装解析到公网地址。"""
        return ["93.184.216.34"]

    monkeypatch.setattr(transport_mod, "_resolve", fake_resolve)
    await check_url_allowed("https://example.com/mcp")


async def test_dns_failure_blocks_connect_but_not_save(monkeypatch):
    """DNS 解析失败：连接期拒，保存期放行。

    解析失败说明的是「此刻查不到这个主机」，不是「这个地址危险」——而
    「此刻查不到」在实际部署里很常见（服务还没上线、内外网分裂 DNS、
    DNS 抖动）。拿它卡保存期，会把最该用的内网 MCP 服务器挡在门外。

    真正连上去之前会再查一次，那时不容情——所以这条路径上没有安全缺口。
    """
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    monkeypatch.setattr(settings, "mcp_allowed_hosts", [])

    async def failing_resolve(_host: str) -> list[str]:
        """假装 DNS 解析失败。"""
        raise McpTransportError("解析主机名失败：intranet.example.com")

    monkeypatch.setattr(transport_mod, "_resolve", failing_resolve)

    # 保存期：放行
    await check_url_allowed("https://intranet.example.com/mcp", require_dns=False)
    # 连接期：拒绝
    with pytest.raises(McpTransportError):
        await check_url_allowed("https://intranet.example.com/mcp")


async def test_ip_literal_private_is_rejected_even_without_dns(monkeypatch):
    """IP 字面量指向私网时，保存期也拒。

    最常见的 SSRF 载荷就是字面量 IP——``127.0.0.1``、``169.254.169.254``。
    它不需要 DNS 就能判定，保存期没有理由放行。
    """
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    monkeypatch.setattr(settings, "mcp_allowed_hosts", [])
    with pytest.raises(McpTransportError) as ei:
        await check_url_allowed("http://169.254.169.254/latest", require_dns=False)
    assert "SSRF" in str(ei.value)

"""MCP 服务器与工具快照的数据访问（设计文档 §21.6）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import McpServer
from app.models.mcp_tool import McpTool


class McpServerRepo:
    """MCP 服务器注册表的读写。"""

    async def create(self, db: AsyncSession, **kwargs) -> McpServer:
        """插入服务器并 commit + refresh。"""
        server = McpServer(**kwargs)
        db.add(server)
        await db.commit()
        await db.refresh(server)
        return server

    async def get_by_code(
        self, db: AsyncSession, code: str, tenant_id: str | None = None
    ) -> McpServer | None:
        """按业务唯一 code 取服务器。"""
        stmt = sa.select(McpServer).where(McpServer.code == code)
        if tenant_id is not None:
            stmt = stmt.where(McpServer.tenant_id == tenant_id)
        return await db.scalar(stmt)

    async def list(
        self,
        db: AsyncSession,
        only_enabled: bool = False,
        limit: int = 200,
        offset: int = 0,
        tenant_id: str | None = None,
    ) -> list[McpServer]:
        """分页列出服务器，按 id 倒序。"""
        stmt = sa.select(McpServer)
        if only_enabled:
            stmt = stmt.where(McpServer.enabled == 1)
        if tenant_id is not None:
            stmt = stmt.where(McpServer.tenant_id == tenant_id)
        stmt = stmt.order_by(McpServer.id.desc()).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def update(
        self,
        db: AsyncSession,
        code: str,
        fields: dict,
        bump_version: bool = True,
        tenant_id: str | None = None,
    ) -> McpServer | None:
        """按 code 局部更新字段（值为 None 的键跳过）。"""
        server = await self.get_by_code(db, code, tenant_id)
        if server is None:
            return None
        for key, value in fields.items():
            if value is not None:
                setattr(server, key, value)
        if bump_version:
            # 原子自增：读出来 +1 再写回在并发更新下会丢版本。
            await db.execute(
                sa.update(McpServer)
                .where(
                    McpServer.code == code,
                    *(
                        [McpServer.tenant_id == tenant_id]
                        if tenant_id is not None
                        else []
                    ),
                )
                .values(version=McpServer.version + 1)
            )
        await db.commit()
        await db.refresh(server)
        return server

    async def delete(
        self, db: AsyncSession, code: str, tenant_id: str | None = None
    ) -> bool:
        """按 code 硬删除，连带删掉它的工具快照。"""
        server = await self.get_by_code(db, code, tenant_id)
        if server is None:
            return False
        await db.execute(
            sa.delete(McpTool).where(McpTool.server_id == server.id)
        )
        await db.delete(server)
        await db.commit()
        return True

    async def map_by_id(
        self,
        db: AsyncSession,
        limit: int = 1000,
        tenant_id: str | None = None,
    ) -> dict[int, McpServer]:
        """服务器 id → 服务器实例的字典。

        工具快照行上存的是 ``server_id``，要拿到服务器的 ``code`` 就得关联一下。
        **不用「从限定名里反推 code」**——限定名的形状是
        ``mcp__<server_code>__<tool>``，反推依赖「code 里不会出现分隔符」这个
        约定；一旦约定破了（code 里带 ``__``），反推出来的就是另一台服务器。
        关联 ``server_id`` 没有这个前提。

        Args:
            db: 请求级 AsyncSession。
            limit: 最多取多少台服务器。

        Returns:
            id → 服务器实例的字典。
        """
        rows = await self.list(db, limit=limit, tenant_id=tenant_id)
        return {row.id: row for row in rows}


class McpToolRepo:
    """工具目录快照的读写。"""

    async def list_by_server(
        self, db: AsyncSession, server_id: int
    ) -> list[McpTool]:
        """列出某台服务器的全部快照工具，按限定名排序。"""
        stmt = (
            sa.select(McpTool)
            .where(McpTool.server_id == server_id)
            .order_by(McpTool.qualified_name)
        )
        return list(await db.scalars(stmt))

    async def list_all(
        self,
        db: AsyncSession,
        limit: int = 2000,
        tenant_id: str | None = None,
    ) -> list[McpTool]:
        """列出全部快照工具（保存期校验与后台勾选框都要用）。"""
        stmt = sa.select(McpTool)
        if tenant_id is not None:
            stmt = stmt.join(
                McpServer, McpServer.id == McpTool.server_id
            ).where(McpServer.tenant_id == tenant_id)
        stmt = stmt.order_by(McpTool.qualified_name).limit(limit)
        return list(await db.scalars(stmt))

    async def get_by_qualified(
        self, db: AsyncSession, qualified_name: str
    ) -> McpTool | None:
        """按限定名取一个工具——这是它的唯一键。"""
        return await db.scalar(
            sa.select(McpTool).where(McpTool.qualified_name == qualified_name)
        )

    async def qualified_names(
        self, db: AsyncSession, tenant_id: str | None = None
    ) -> set[str]:
        """全部已发现的限定名集合。

        保存期校验要用它：``agent.tools`` 里的 ``mcp__`` 名字要在这里查得到。
        """
        stmt = sa.select(McpTool.qualified_name)
        if tenant_id is not None:
            stmt = stmt.join(
                McpServer, McpServer.id == McpTool.server_id
            ).where(McpServer.tenant_id == tenant_id)
        rows = await db.scalars(stmt)
        return set(rows)

    async def replace_for_server(
        self, db: AsyncSession, server_id: int, rows: list[dict]
    ) -> list[McpTool]:
        """**整体替换**某台服务器的快照。

        快照是**全量替换**而不是增量合并——增量合并会留下服务器已经删掉的工具，
        于是那些工具会一直躺在勾选框里，而点下去必然调用失败。

        Args:
            db: 请求级 AsyncSession。
            server_id: 服务器 id。
            rows: 新的快照行。

        Returns:
            写入后的快照行。
        """
        await db.execute(sa.delete(McpTool).where(McpTool.server_id == server_id))
        out: list[McpTool] = []
        for row in rows:
            item = McpTool(server_id=server_id, **row)
            db.add(item)
            out.append(item)
        await db.commit()
        for item in out:
            await db.refresh(item)
        return out


mcp_server_repo = McpServerRepo()
mcp_tool_repo = McpToolRepo()

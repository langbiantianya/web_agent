"""Skill 数据访问（设计文档 §22.5）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.skill import Skill, SkillResource, SkillRevision


class SkillRepo:
    """技能表的读写。

    与其它 repo 同一套口径：**类 + 模块级单例**，异步方法，``db`` 是首参。

    几处刻意的取舍：

    - ``update`` 的版本自增用 **SQL 表达式** ``version = version + 1``，不读出来
      再 +1 写回——并发下会丢版本（理由见 :mod:`app.repo.workflow_repo`）。
    - ``delete`` **手动级联**删资源与修订历史。项目其它表也没建外键，靠 ORM 级联
      会在跨会话时留下孤儿行。
    - ``count_agents_using`` 在 Python 侧数：JSON 列的跨库查询表达式不统一，
      而 Agent 数量是配置级、量级很小，取回来数最省事也最稳。
    """

    async def create(self, db: AsyncSession, **kwargs) -> Skill:
        """插入技能并 commit + refresh。"""
        skill = Skill(**kwargs)
        db.add(skill)
        await db.commit()
        await db.refresh(skill)
        return skill

    async def get_by_code(self, db: AsyncSession, code: str) -> Skill | None:
        """按业务唯一 code 取技能。"""
        return await db.scalar(sa.select(Skill).where(Skill.code == code))

    async def list(
        self, db: AsyncSession, only_enabled: bool = False, limit: int = 200, offset: int = 0
    ) -> list[Skill]:
        """分页列出技能，按 id 倒序。"""
        stmt = sa.select(Skill)
        if only_enabled:
            stmt = stmt.where(Skill.enabled == 1)
        stmt = stmt.order_by(Skill.id.desc()).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def list_by_codes(self, db: AsyncSession, codes: list[str]) -> list[Skill]:
        """按 code 集合取技能，**按传入顺序返回**。

        顺序很重要：技能正文进提示词时的先后 = 运营在后台排的优先级。
        ``IN`` 查询本身不保证顺序，所以取回来后按传入的 code 列表重排。

        Args:
            db: 请求级 AsyncSession。
            codes: 技能 code 列表。

        Returns:
            找到的技能（找不到的 code 直接略过，不报错——读路径要宽容）。
        """
        if not codes:
            return []
        rows = list(
            await db.scalars(sa.select(Skill).where(Skill.code.in_(codes)))
        )
        by_code = {row.code: row for row in rows}
        return [by_code[c] for c in codes if c in by_code]

    async def update(
        self, db: AsyncSession, code: str, fields: dict, bump_version: bool = True
    ) -> Skill | None:
        """按 code 局部更新字段（值为 None 的键跳过）；不存在时返回 None。"""
        skill = await self.get_by_code(db, code)
        if skill is None:
            return None
        for key, value in fields.items():
            if value is not None:
                setattr(skill, key, value)
        if bump_version:
            # 原子自增：读出来 +1 再写回在并发更新下会丢版本。
            await db.execute(
                sa.update(Skill)
                .where(Skill.code == code)
                .values(version=Skill.version + 1)
            )
        await db.commit()
        await db.refresh(skill)
        return skill

    async def delete(self, db: AsyncSession, code: str) -> bool:
        """按 code 硬删除技能，连资源与修订历史一起删。

        不建外键，所以级联要自己做——留着孤儿行会让「这个技能还有几个资源」
        这种问题在别的路径上答错。

        Args:
            db: 请求级 AsyncSession。
            code: 技能 code。

        Returns:
            删掉了返回 True；本来就没有返回 False（由 API 层转 404）。
        """
        skill = await self.get_by_code(db, code)
        if skill is None:
            return False
        await db.execute(
            sa.delete(SkillResource).where(SkillResource.skill_id == skill.id)
        )
        await db.execute(
            sa.delete(SkillRevision).where(SkillRevision.skill_id == skill.id)
        )
        await db.delete(skill)
        await db.commit()
        return True

    # ------------------------------------------------------------- 资源

    async def list_resources(self, db: AsyncSession, skill_id: int) -> list[SkillResource]:
        """列出技能的全部资源，按 path 排序。"""
        stmt = (
            sa.select(SkillResource)
            .where(SkillResource.skill_id == skill_id)
            .order_by(SkillResource.path)
        )
        return list(await db.scalars(stmt))

    async def get_resource(
        self, db: AsyncSession, skill_id: int, path: str
    ) -> SkillResource | None:
        """按 (skill_id, path) 取一个资源——这是资源的唯一键。"""
        return await db.scalar(
            sa.select(SkillResource).where(
                SkillResource.skill_id == skill_id,
                SkillResource.path == path,
            )
        )

    async def put_resource(
        self,
        db: AsyncSession,
        skill_id: int,
        path: str,
        content: bytes,
        mime_type: str,
        is_text: int,
    ) -> SkillResource:
        """写一个资源：同一个 path 写两次是**覆盖**而不是新增。

        Args:
            db: 请求级 AsyncSession。
            skill_id: 所属技能 id。
            path: 资源相对路径（调用方已校验过格式）。
            content: 资源字节。
            mime_type: mime 类型。
            is_text: 是不是文本（决定它能不能进提示词、能不能回滚）。

        Returns:
            写入后的资源行。
        """
        row = await self.get_resource(db, skill_id, path)
        if row is None:
            row = SkillResource(
                skill_id=skill_id,
                path=path,
                mime_type=mime_type,
                is_text=is_text,
                content=content,
                size_bytes=len(content),
            )
            db.add(row)
        else:
            row.mime_type = mime_type
            row.is_text = is_text
            row.content = content
            row.size_bytes = len(content)
        await db.commit()
        await db.refresh(row)
        return row

    async def delete_resource(self, db: AsyncSession, skill_id: int, path: str) -> bool:
        """删一个资源；本来就没有返回 False。"""
        row = await self.get_resource(db, skill_id, path)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
        return True

    async def total_resource_bytes(
        self, db: AsyncSession, skill_id: int, exclude_path: str | None = None
    ) -> int:
        """技能的资源总量（字节），用于「单技能资源总量上限」的校验。

        Args:
            db: 请求级 AsyncSession。
            skill_id: 技能 id。
            exclude_path: 排除某个路径——**替换**一个资源时，它自己的旧大小
                不该算进总量，否则「先删后加」和「原地覆盖」会差一个旧文件。
        """
        stmt = sa.select(sa.func.coalesce(sa.func.sum(SkillResource.size_bytes), 0)).where(
            SkillResource.skill_id == skill_id
        )
        if exclude_path is not None:
            stmt = stmt.where(SkillResource.path != exclude_path)
        return int(await db.scalar(stmt) or 0)

    # ------------------------------------------------------------- 修订

    async def add_revision(
        self,
        db: AsyncSession,
        skill_id: int,
        version: int,
        body: str,
        resources: list | None,
        author: str,
        note: str,
    ) -> SkillRevision:
        """记一条变更快照。"""
        revision = SkillRevision(
            skill_id=skill_id,
            version=version,
            body=body,
            resources=resources,
            author=author,
            note=note,
        )
        db.add(revision)
        await db.commit()
        await db.refresh(revision)
        return revision

    async def list_revisions(self, db: AsyncSession, skill_id: int) -> list[SkillRevision]:
        """列出变更历史，版本号倒序（最近的在前）。"""
        stmt = (
            sa.select(SkillRevision)
            .where(SkillRevision.skill_id == skill_id)
            .order_by(SkillRevision.version.desc())
        )
        return list(await db.scalars(stmt))

    async def get_revision(
        self, db: AsyncSession, skill_id: int, version: int
    ) -> SkillRevision | None:
        """按版本号取一条快照——回滚就是把它读出来再写回去。"""
        return await db.scalar(
            sa.select(SkillRevision).where(
                SkillRevision.skill_id == skill_id,
                SkillRevision.version == version,
            )
        )

    # ------------------------------------------------------------- 引用

    async def count_agents_using(self, db: AsyncSession, code: str) -> int:
        """有几个 Agent 绑着这个技能。

        在 Python 侧数：``agent.skills`` 是 JSON 列，三种方言的 JSON 表达式语法
        各不相同（MySQL 的 ``JSON_CONTAINS`` / PG 的 ``@>`` / SQLite 的
        ``json_each``），写成跨库通用 SQL 反而不现实。Agent 是配置级、量级很小，
        取回列表在 Python 里数最稳。

        Args:
            db: 请求级 AsyncSession。
            code: 技能 code。

        Returns:
            绑着它的 Agent 数量。
        """
        rows = await db.scalars(sa.select(Agent.skills))
        count = 0
        for raw in rows:
            if isinstance(raw, list) and code in raw:
                count += 1
        return count


skill_repo = SkillRepo()

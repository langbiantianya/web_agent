"""Skill 管理路由（设计文档 §22.11）。

分两套：

- **只读**（``GET``）——普通运营可用：看技能、看资源、导出、查变更历史；
- **写入**——需要 `skill.write` Casbin 权限（:data:`app.api.deps.SkillWriteDep`）。技能正文会进
  prompt，而 prompt 就是模型的行为，所以「谁能写技能」≈「谁能改模型的行为」。

## 为什么资源路径这么难搞

技能进了数据库之后，资源路径**不是文件系统路径，是查表的键**。所以这里没有
``resolve()``、没有符号链接检查、没有 ``..`` 拼接——``../../etc/passwd`` 查不到
任何行就什么也读不到。剩下的只是**格式规整**（字符集 / 长度 / 段数）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from fastapi import APIRouter, Body, File, Header, Query, Response, UploadFile

from app.api.deps import DbSession, SkillReadDep, SkillWriteDep
from app.core.admin_auth import AdminPrincipal
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.skill_import import (
    SKILL_MD_NAME,
    SkillImportError,
    build_skill_md_zip,
    extract_zip_safely,
    parse_skill_package,
)
from app.core.skill_pack import (
    MAX_RESOURCE_BYTES,
    MAX_SKILL_RESOURCE_BYTES,
    SkillFrontmatter,
    compute_content_hash,
    guess_mime_type,
    is_text_path,
    render_skill_md,
)
from app.core.skill_paths import SkillPathError, validate_resource_path
from app.core.skill_risk import scan_skill_risks
from app.core.skill_runtime import (
    REQUIRED_ISOLATED_STUDIO_VERSION,
    SkillRuntimeError,
    parse_skill_runtime,
    runtime_from_skill,
)
from app.repo.skill_repo import skill_repo
from app.schemas.skill import (
    SkillCreate,
    SkillImportIn,
    SkillOut,
    SkillPublishIn,
    SkillResourceOut,
    SkillRevisionOut,
    SkillRollbackIn,
    SkillSummaryOut,
    SkillUpdate,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


# ------------------------------------------------------------------ helpers


def _sha256(data: bytes) -> str:
    """内容的 sha256（十六进制）。"""
    return hashlib.sha256(data).hexdigest()


def _validate_runtime_metadata(metadata: dict | None) -> None:
    """手工创建/更新技能时，运行档声明也要在写路径上校验。

    导入路径已经由 :func:`app.core.skill_pack.parse_skill_md` 校验；这里是
    另一条写路径（表单 / API），不能让它绕过。

    Args:
        metadata: 技能的 metadata。

    Raises:
        BadRequestError: 运行档声明不合法。
    """
    try:
        parse_skill_runtime(metadata)
    except SkillRuntimeError as exc:
        raise BadRequestError(f"运行档声明不合法：{exc}") from exc


def _runtime_status(skill) -> tuple[str, str, bool, str]:
    """计算技能运行档在当前部署是否可用。"""
    try:
        runtime = runtime_from_skill(skill)
    except SkillRuntimeError as exc:
        return "invalid", "", False, f"运行档声明非法：{exc}"
    if not runtime.is_isolated:
        return runtime.profile, runtime.image, True, runtime.describe()

    from app.core.studio import get_studio_client, version_at_least

    client = get_studio_client()
    health = client.health_info if client is not None else None
    if health is None:
        return runtime.profile, runtime.image, False, "studio 未启用或未连接"
    if not getattr(health, "supports_isolated", False):
        return runtime.profile, runtime.image, False, "studio 没有可用 sandbox 运行时"
    if not version_at_least(
        getattr(health, "version", ""), REQUIRED_ISOLATED_STUDIO_VERSION
    ):
        return (
            runtime.profile,
            runtime.image,
            False,
            f"需要 studio >= {REQUIRED_ISOLATED_STUDIO_VERSION}",
        )
    if runtime.image and health.image(runtime.image) is None:
        return runtime.profile, runtime.image, False, f"镜像「{runtime.image}」未登记"
    detail = runtime.describe()
    if runtime.image:
        image = health.image(runtime.image)
        if image is not None and getattr(image, "digest", ""):
            detail += f"，digest={image.digest}"
    return runtime.profile, runtime.image, True, detail


def _to_out(
    skill,
    used_by: list[str] | None = None,
    *,
    import_warnings: list[str] | None = None,
) -> SkillOut:
    """ORM ``Skill`` → 对外响应模型。

    库表列大多可空，而响应模型里这些字段是必填非空——这里统一兜默认值，
    免得历史数据里一个 NULL 就让整个列表接口 500。

    Args:
        skill: ORM ``Skill`` 实例。
        used_by: 绑着它的 Agent code 列表。

    Returns:
        ``SkillOut``。
    """
    profile, image, available, detail = _runtime_status(skill)
    return SkillOut(
        id=skill.id,
        code=skill.code,
        tenant_id=skill.tenant_id or "default",
        name=skill.name or skill.code,
        description=skill.description or "",
        license=skill.license or "",
        compatibility=skill.compatibility or "",
        metadata=skill.metadata_,
        allowed_tools=skill.allowed_tools or "",
        body=skill.body or "",
        origin=skill.origin or "manual",
        content_hash=skill.content_hash or "",
        enabled=bool(skill.enabled),
        status=skill.status or "published",
        deleted_at=skill.deleted_at,
        version=skill.version or 1,
        runtime_profile=profile,
        runtime_image=image,
        runtime_available=available,
        runtime_detail=detail,
        import_warnings=import_warnings or [],
        used_by=used_by or [],
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


async def _used_by_map(db, tenant_id: str = "default") -> dict[str, list[str]]:
    """每个技能被哪些 Agent 绑着。

    在 Python 侧统计：``agent.skills`` 是 JSON 列，三种方言的 JSON 表达式语法
    各不相同，写跨库通用 SQL 反而不现实，而 Agent 是配置级、量级很小。
    """
    from app.repo.agent_repo import agent_repo

    agents = [
        row
        for row in await agent_repo.list(db, limit=1000)
        if (row.tenant_id or "default") == tenant_id
    ]
    out: dict[str, list[str]] = {}
    for agent in agents:
        raw = agent.skills
        if not isinstance(raw, list):
            continue
        for code in raw:
            if isinstance(code, str) and code:
                out.setdefault(code, []).append(agent.code)
    return out


def _tenant_of(admin: AdminPrincipal | None) -> str:
    """公开读使用默认租户，管理员读使用其绑定租户。"""
    return admin.tenant_id if admin is not None else "default"


def _operator_name(admin: AdminPrincipal) -> str:
    """审计作者统一记录认证主体的用户名。"""
    return admin.username


async def _hash_for(db, skill, body: str | None = None) -> str:
    """算这个技能（可能换了正文）的内容指纹。

    口径：``sha256(body ‖ 各资源按 path 排序的摘要)``。任何一次写入（改正文、
    换资源）之后都要重算，否则指纹与内容对不上，变更检测就失效了。

    Args:
        db: 请求级 AsyncSession。
        skill: ORM ``Skill`` 实例。
        body: 新的正文；``None`` 表示正文不变。

    Returns:
        64 位十六进制 sha256。
    """
    rows = await skill_repo.list_resources(db, skill.id)
    text = skill.body if body is None else body
    return compute_content_hash(text or "", [(r.path, r.content) for r in rows])


async def _write_revision(db, skill, author: str, note: str) -> None:
    """记一条变更快照。

    摘要信息保留在审计 JSON 中，同时把全部资源正文复制到独立修订资源表；文本
    与二进制都能从快照恢复。技能进了 DB 就没有 git 了，改错了要能退回去。
    """
    rows = await skill_repo.list_resources(db, skill.id)
    resources: list[dict] = []
    resource_blobs: list[dict] = []
    for row in rows:
        item = {
            "path": row.path,
            "sha256": _sha256(row.content),
            "is_text": bool(row.is_text),
            "size_bytes": row.size_bytes,
        }
        if row.is_text:
            item["text"] = row.content.decode("utf-8", errors="replace")
        resources.append(item)
        resource_blobs.append(
            {
                "path": row.path,
                "mime_type": row.mime_type,
                "is_text": bool(row.is_text),
                "content": row.content,
            }
        )
    await skill_repo.add_revision(
        db,
        skill.id,
        skill.version,
        skill.body or "",
        resources,
        author,
        note,
        resource_blobs=resource_blobs,
    )


def _frontmatter_of(skill) -> SkillFrontmatter:
    """从库里的一行取出 frontmatter 字段。"""
    return SkillFrontmatter(
        name=skill.code,
        description=skill.description or "",
        license=skill.license or "",
        compatibility=skill.compatibility or "",
        metadata=skill.metadata_ or {},
        allowed_tools=skill.allowed_tools or "",
    )


# ------------------------------------------------------------------ 只读


@router.get("", response_model=list[SkillSummaryOut])
async def list_skills(
    db: DbSession,
    admin: SkillReadDep,
    only_enabled: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[SkillSummaryOut]:
    """技能列表。带上被哪些 Agent 绑定——删技能前得先看一眼这个。"""
    tenant_id = _tenant_of(admin)
    rows = await skill_repo.list(
        db,
        only_enabled=only_enabled,
        limit=limit,
        offset=offset,
        tenant_id=tenant_id,
    )
    used_by = await _used_by_map(db, tenant_id)
    out: list[SkillSummaryOut] = []
    for skill in rows:
        profile, image, available, detail = _runtime_status(skill)
        out.append(
            SkillSummaryOut(
                code=skill.code,
                tenant_id=skill.tenant_id,
                name=skill.name or skill.code,
                description=skill.description or "",
                origin=skill.origin or "manual",
                version=skill.version or 1,
                content_hash=skill.content_hash or "",
                enabled=bool(skill.enabled),
                status=skill.status,
                allowed_tools=skill.allowed_tools or "",
                runtime_profile=profile,
                runtime_image=image,
                runtime_available=available,
                runtime_detail=detail,
                used_by=used_by.get(skill.code, []),
            )
        )
    return out


@router.get("/{code}", response_model=SkillOut)
async def get_skill(
    code: str, db: DbSession, admin: SkillReadDep
) -> SkillOut:
    """技能详情（含正文与 frontmatter 字段）。不存在返回 404。"""
    tenant_id = _tenant_of(admin)
    skill = await skill_repo.get_by_code(db, code, tenant_id)
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    used_by = await _used_by_map(db, tenant_id)
    return _to_out(skill, used_by.get(code, []))


@router.get("/{code}/resources", response_model=list[SkillResourceOut])
async def list_skill_resources(
    code: str, db: DbSession, admin: SkillReadDep
) -> list[SkillResourceOut]:
    """资源清单（path / 大小 / mime / sha256）。"""
    skill = await skill_repo.get_by_code(db, code, _tenant_of(admin))
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    rows = await skill_repo.list_resources(db, skill.id)
    return [
        SkillResourceOut(
            path=r.path,
            size_bytes=r.size_bytes,
            mime_type=r.mime_type,
            is_text=bool(r.is_text),
            sha256=_sha256(r.content),
        )
        for r in rows
    ]


@router.get("/{code}/resources/{path:path}")
async def get_skill_resource(
    code: str, path: str, db: DbSession, admin: SkillReadDep
) -> Response:
    """单个资源的内容。

    文本直接返回；二进制走 ``Content-Disposition: attachment``（浏览器会下载
    而不是试图渲染）。
    """
    skill = await skill_repo.get_by_code(db, code, _tenant_of(admin))
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    try:
        cleaned = validate_resource_path(path)
    except SkillPathError as exc:
        raise BadRequestError(str(exc)) from exc
    row = await skill_repo.get_resource(db, skill.id, cleaned)
    if row is None:
        raise NotFoundError(f"技能「{code}」里没有资源「{cleaned}」")
    if row.is_text:
        return Response(
            content=row.content.decode("utf-8", errors="replace"),
            media_type=row.mime_type or "text/plain; charset=utf-8",
        )
    return Response(
        content=row.content,
        media_type=row.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{cleaned.rsplit("/", 1)[-1]}"'
        },
    )


@router.get("/{code}/export")
async def export_skill(
    code: str,
    db: DbSession,
    admin: SkillReadDep,
    format: str = Query("zip", pattern="^(zip|md)$"),
) -> Response:
    """导出标准技能包。

    ``format=zip``（默认）导出的是一个能直接被 ``POST /api/skills/import`` 吃
    进去的 zip——``import(export(x)) == x`` 是不变式。``format=md`` 只给
    SKILL.md 原文，给「看一眼正文」用。

    ``SKILL.md`` 是**拼出来的**：frontmatter 来自库的各列，正文来自 ``body``。
    技能不存文件，所以导出物是唯一的存在形式。
    """
    skill = await skill_repo.get_by_code(db, code, _tenant_of(admin))
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    skill_md = render_skill_md(_frontmatter_of(skill), skill.body or "")
    if format == "md":
        return Response(content=skill_md, media_type="text/markdown; charset=utf-8")
    rows = await skill_repo.list_resources(db, skill.id)
    data = build_skill_md_zip(skill_md, [(r.path, r.content) for r in rows])
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{code}.zip"'},
    )


@router.get("/{code}/revisions", response_model=list[SkillRevisionOut])
async def list_skill_revisions(
    code: str, db: DbSession, admin: SkillReadDep
) -> list[SkillRevisionOut]:
    """变更历史（版本 / 作者 / 说明 / 时间），最近的在前。"""
    skill = await skill_repo.get_by_code(db, code, _tenant_of(admin))
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    rows = await skill_repo.list_revisions(db, skill.id)
    return [
        SkillRevisionOut(
            version=r.version,
            author=r.author or "",
            note=r.note or "",
            resource_count=len(r.resources or []),
            binary_resource_count=sum(
                1
                for item in (r.resources or [])
                if isinstance(item, dict) and item.get("text") is None
            ),
            created_at=r.created_at,
        )
        for r in rows
    ]


# ------------------------------------------------------------------ 写入


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(
    payload: SkillCreate, db: DbSession, author: SkillWriteDep
) -> SkillOut:
    """创建技能。``origin='manual'``，并写一条审计快照。"""
    tenant_id = author.tenant_id
    if await skill_repo.get_by_code(
        db, payload.code, tenant_id, include_deleted=True
    ):
        raise ConflictError(f"技能 code 已存在：{payload.code}")
    if payload.name and payload.name != payload.code:
        # 规范要求 frontmatter 的 name 等于目录名，而目录名在这里就是 code。
        # 存一个不一样的 name，导出的 SKILL.md 就不是合规的了。
        raise BadRequestError(
            f"name 必须与 code 一致（规范要求 name 等于目录名）："
            f"code={payload.code}，name={payload.name}"
        )
    _validate_runtime_metadata(payload.metadata)
    skill = await skill_repo.create(
        db,
        code=payload.code,
        tenant_id=tenant_id,
        name=payload.code,
        description=payload.description,
        license=payload.license,
        compatibility=payload.compatibility,
        metadata_=payload.metadata,
        allowed_tools=payload.allowed_tools,
        body=payload.body,
        origin="manual",
        content_hash=compute_content_hash(payload.body, []),
        status=payload.status,
        enabled=1 if payload.enabled and payload.status == "published" else 0,
    )
    await _write_revision(db, skill, _operator_name(author), "创建")
    used_by = await _used_by_map(db, tenant_id)
    return _to_out(skill, used_by.get(skill.code, []))


@router.put("/{code}", response_model=SkillOut)
async def update_skill(
    code: str, payload: SkillUpdate, db: DbSession, author: SkillWriteDep
) -> SkillOut:
    """更新技能：``version+1``，重算指纹，写审计。"""
    tenant_id = author.tenant_id
    skill = await skill_repo.get_by_code(db, code, tenant_id)
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    fields = payload.model_dump(exclude_unset=True)
    # 仓储层的约定是「值为 None 的键跳过」，所以要清空的字段得在这里换掉：
    # 「不传」与「清空」是两件事。
    if "metadata" in fields and fields["metadata"] is None:
        fields["metadata"] = {}
    if "metadata" in fields:
        _validate_runtime_metadata(fields["metadata"])
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    target_status = fields.get("status", skill.status)
    if target_status == "draft":
        fields["enabled"] = 0
    used_by = await _used_by_map(db, tenant_id)
    disables_active_skill = bool(skill.enabled) and (
        ("enabled" in fields and not fields["enabled"])
        or target_status == "draft"
    )
    if code in used_by and disables_active_skill:
        raise ConflictError(
            f"技能「{code}」仍被这些 Agent 使用：{'、'.join(used_by[code])}。"
            "先解绑再停用或设为草稿。"
        )
    if "body" in fields:
        # 正文变了 → 指纹要一起改。指纹与版本号在**同一次更新**里落库，
        # 这样任何一个版本号都唯一对应一份内容（修订快照按版本号唯一）。
        fields["content_hash"] = await _hash_for(db, skill, fields["body"])
    skill = await skill_repo.update(
        db, code, fields, bump_version=True, tenant_id=tenant_id
    )
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    await _write_revision(db, skill, _operator_name(author), "更新")
    return _to_out(skill, used_by.get(code, []))


@router.delete("/{code}", status_code=204)
async def delete_skill(
    code: str,
    db: DbSession,
    author: SkillWriteDep,
) -> None:
    """归档技能并保留资源与修订历史。

    删除是软删除。绑着它的 Agent 仍视为冲突，避免模型行为静默变化；解绑后可
    归档，之后通过恢复接口把它置为草稿。
    """
    tenant_id = author.tenant_id
    skill = await skill_repo.get_by_code(db, code, tenant_id)
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    used_by = await _used_by_map(db, tenant_id)
    bound = used_by.get(code, [])
    if bound:
        raise ConflictError(
            f"技能「{code}」还被这些 Agent 绑着：{'、'.join(bound)}。"
            "先解绑或停用，再删。"
        )
    if not await skill_repo.soft_delete(db, code, tenant_id):
        raise NotFoundError(f"技能不存在：{code}")


@router.post("/{code}/restore", response_model=SkillOut)
async def restore_skill(
    code: str, db: DbSession, author: SkillWriteDep
) -> SkillOut:
    """恢复归档技能为草稿；再次发布前不会注入任何 Agent。"""
    skill = await skill_repo.restore(db, code, author.tenant_id)
    if skill is None:
        raise NotFoundError(f"已归档技能不存在：{code}")
    await _write_revision(db, skill, _operator_name(author), "恢复为草稿")
    return _to_out(skill, [])


@router.post("/{code}/publish", response_model=SkillOut)
async def publish_skill(
    code: str,
    payload: SkillPublishIn,
    db: DbSession,
    author: SkillWriteDep,
) -> SkillOut:
    """把草稿发布为可注入版本；可显式发布但不启用。"""
    skill = await skill_repo.update(
        db,
        code,
        {
            "status": "published",
            "enabled": 1 if payload.enabled else 0,
        },
        bump_version=True,
        tenant_id=author.tenant_id,
    )
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    await _write_revision(
        db, skill, _operator_name(author), "发布草稿"
    )
    used_by = await _used_by_map(db, author.tenant_id)
    return _to_out(skill, used_by.get(code, []))


@router.post("/{code}/resources", response_model=SkillResourceOut, status_code=201)
@router.put("/{code}/resources", response_model=SkillResourceOut, status_code=201)
async def put_skill_resource(
    code: str,
    db: DbSession,
    author: SkillWriteDep,
    body: bytes = Body(
        ...,
        media_type="application/octet-stream",
        description="资源内容（原始字节）。请求的 Content-Type 不要用 application/json。",
    ),
    path: str = Query("", description="资源相对路径（也可放 X-Resource-Path 头）"),
    x_resource_path: str | None = Header(
        default=None, alias="X-Resource-Path", description="资源相对路径"
    ),
) -> SkillResourceOut:
    """上传或替换一个资源文件。

    请求体是**原始字节**（不是 JSON）——所以客户端要把 ``Content-Type`` 设成
    ``application/octet-stream`` 之类，设成 ``application/json`` 的话 FastAPI
    会拿 JSON 去解析它然后报 400。

    路径放 query 参数或 ``X-Resource-Path`` 头里都行，query 优先。
    ``POST`` 保留给旧客户端；管理页使用幂等语义更明确的 ``PUT``。
    """
    tenant_id = author.tenant_id
    skill = await skill_repo.get_by_code(db, code, tenant_id)
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    raw_path = path or x_resource_path or ""
    try:
        cleaned = validate_resource_path(raw_path)
    except SkillPathError as exc:
        raise BadRequestError(str(exc)) from exc
    if len(body) > MAX_RESOURCE_BYTES:
        raise BadRequestError(
            f"资源超过 {MAX_RESOURCE_BYTES} 字节上限（收到 {len(body)} 字节）"
        )
    others = await skill_repo.total_resource_bytes(db, skill.id, exclude_path=cleaned)
    if others + len(body) > MAX_SKILL_RESOURCE_BYTES:
        raise BadRequestError(
            f"技能资源总量超过 {MAX_SKILL_RESOURCE_BYTES} 字节上限"
        )
    row = await skill_repo.put_resource(
        db,
        skill.id,
        cleaned,
        body,
        guess_mime_type(cleaned),
        1 if is_text_path(cleaned) else 0,
    )
    # 资源变了 → 内容变了 → 指纹与版本号一起往前走。
    skill = await skill_repo.update(
        db,
        code,
        {"content_hash": await _hash_for(db, skill)},
        bump_version=True,
        tenant_id=tenant_id,
    )
    await _write_revision(
        db, skill, _operator_name(author), f"写入资源 {cleaned}"
    )
    return SkillResourceOut(
        path=row.path,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        is_text=bool(row.is_text),
        sha256=_sha256(row.content),
    )


@router.delete("/{code}/resources/{path:path}", status_code=204)
async def delete_skill_resource(
    code: str, path: str, db: DbSession, author: SkillWriteDep
) -> None:
    """删除一个资源文件。"""
    tenant_id = author.tenant_id
    skill = await skill_repo.get_by_code(db, code, tenant_id)
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    try:
        cleaned = validate_resource_path(path)
    except SkillPathError as exc:
        raise BadRequestError(str(exc)) from exc
    if not await skill_repo.delete_resource(db, skill.id, cleaned):
        raise NotFoundError(f"技能「{code}」里没有资源「{cleaned}」")
    skill = await skill_repo.update(
        db,
        code,
        {"content_hash": await _hash_for(db, skill)},
        bump_version=True,
        tenant_id=tenant_id,
    )
    await _write_revision(
        db, skill, _operator_name(author), f"删除资源 {cleaned}"
    )


@router.post("/import", response_model=SkillOut, status_code=201)
async def import_skill(
    db: DbSession,
    author: SkillWriteDep,
    file: UploadFile = File(..., description="技能包 zip"),
) -> SkillOut:
    """从 zip 导入一个技能。

    ``origin='import'``，且**默认 ``enabled=0``**：导入是唯一一条内容来源不可信
    的入口，别人的技能包进了库就能直接改模型的行为——先停用，管理员看过内容
    再启用。

    zip 的四道防护（zip slip / 符号链接 / 条目数与体积 / 压缩比）在
    :func:`app.core.skill_import.extract_zip_safely` 里。
    """
    data = await file.read()
    try:
        entries = extract_zip_safely(data)
        package = parse_skill_package(entries)
    except SkillImportError as exc:
        raise BadRequestError(str(exc)) from exc

    code = package.frontmatter.name
    tenant_id = author.tenant_id
    if await skill_repo.get_by_code(db, code, tenant_id):
        raise ConflictError(f"技能 code 已存在：{code}")

    total = sum(len(c) for _, c in package.resources)
    if total > MAX_SKILL_RESOURCE_BYTES:
        raise BadRequestError(
            f"技能资源总量超过 {MAX_SKILL_RESOURCE_BYTES} 字节上限"
        )

    warnings = scan_skill_risks(package.body, package.resources)
    skill = await skill_repo.create(
        db,
        code=code,
        tenant_id=tenant_id,
        name=code,
        description=package.frontmatter.description,
        license=package.frontmatter.license,
        compatibility=package.frontmatter.compatibility,
        metadata_=package.frontmatter.metadata or None,
        allowed_tools=package.frontmatter.allowed_tools,
        body=package.body,
        origin="import",
        content_hash="",
        status="draft",
        enabled=0,
    )
    for path, content in package.resources:
        await skill_repo.put_resource(
            db,
            skill.id,
            path,
            content,
            guess_mime_type(path),
            1 if is_text_path(path) else 0,
        )
    # 技能是刚建的（version=1），所以这里**不再自增**——修订快照按版本号唯一，
    # 再加一版就会撞 (skill_id, version)。
    skill = await skill_repo.update(
        db,
        code,
        {"content_hash": await _hash_for(db, skill)},
        bump_version=False,
        tenant_id=tenant_id,
    )
    await _write_revision(db, skill, _operator_name(author), "导入")
    return _to_out(skill, [], import_warnings=warnings)


@router.post("/import/entries", response_model=SkillOut, status_code=201)
async def import_skill_entries(
    payload: SkillImportIn, db: DbSession, author: SkillWriteDep
) -> SkillOut:
    """从**目录结构**（JSON）导入一个技能。

    走的是**同一套**路径格式校验，只是省掉了 zip 解压那一层——后台批量粘贴
    与脚本调用用这个入口。
    """
    entries: list[tuple[str, bytes]] = []
    for item in payload.entries:
        try:
            cleaned = validate_resource_path(item.path)
        except SkillPathError as exc:
            raise BadRequestError(str(exc)) from exc
        try:
            content = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadRequestError(f"资源「{cleaned}」的 base64 内容不合法") from exc
        if len(content) > MAX_RESOURCE_BYTES:
            raise BadRequestError(
                f"资源「{cleaned}」超过 {MAX_RESOURCE_BYTES} 字节上限"
            )
        entries.append((cleaned, content))

    skill_md: bytes | None = None
    resources: list[tuple[str, bytes]] = []
    for path, content in entries:
        if path == SKILL_MD_NAME:
            skill_md = content
        else:
            resources.append((path, content))
    if skill_md is None:
        raise BadRequestError(f"包里没有 {SKILL_MD_NAME}")

    from app.core.skill_pack import SkillPackError, parse_skill_md

    try:
        parsed = parse_skill_md(skill_md.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BadRequestError(f"{SKILL_MD_NAME} 不是合法的 UTF-8 文本") from exc
    except SkillPackError as exc:
        raise BadRequestError(f"{SKILL_MD_NAME} 解析失败：{exc}") from exc

    code = parsed.frontmatter.name
    tenant_id = author.tenant_id
    if await skill_repo.get_by_code(db, code, tenant_id):
        raise ConflictError(f"技能 code 已存在：{code}")
    total = sum(len(c) for _, c in resources)
    if total > MAX_SKILL_RESOURCE_BYTES:
        raise BadRequestError(
            f"技能资源总量超过 {MAX_SKILL_RESOURCE_BYTES} 字节上限"
        )

    warnings = scan_skill_risks(parsed.body, resources)
    skill = await skill_repo.create(
        db,
        code=code,
        tenant_id=tenant_id,
        name=code,
        description=parsed.frontmatter.description,
        license=parsed.frontmatter.license,
        compatibility=parsed.frontmatter.compatibility,
        metadata_=parsed.frontmatter.metadata or None,
        allowed_tools=parsed.frontmatter.allowed_tools,
        body=parsed.body,
        origin="import",
        content_hash="",
        status="published" if payload.enabled else "draft",
        enabled=1 if payload.enabled else 0,
    )
    for path, content in resources:
        await skill_repo.put_resource(
            db,
            skill.id,
            path,
            content,
            guess_mime_type(path),
            1 if is_text_path(path) else 0,
        )
    skill = await skill_repo.update(
        db,
        code,
        {"content_hash": await _hash_for(db, skill)},
        bump_version=False,
        tenant_id=tenant_id,
    )
    await _write_revision(
        db, skill, _operator_name(author), "导入（目录结构）"
    )
    return _to_out(skill, [], import_warnings=warnings)


@router.post("/{code}/rollback", response_model=SkillOut)
async def rollback_skill(
    code: str, payload: SkillRollbackIn, db: DbSession, author: SkillWriteDep
) -> SkillOut:
    """回滚到指定历史版本。

    正文与全部资源（包括二进制）一起恢复。恢复后的技能进入草稿态，避免历史
    内容未经重新发布就直接影响线上模型。
    """
    tenant_id = author.tenant_id
    skill = await skill_repo.get_by_code(db, code, tenant_id)
    if skill is None:
        raise NotFoundError(f"技能不存在：{code}")
    revision = await skill_repo.get_revision(db, skill.id, payload.version)
    if revision is None:
        raise NotFoundError(f"技能「{code}」没有版本 {payload.version} 的快照")

    blobs = await skill_repo.get_revision_resources(db, revision.id)
    if not blobs:
        # 兼容升级前产生的修订：旧快照仍可从 JSON 恢复文本资源。
        blobs = []
        for item in revision.resources or []:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str):
                blobs.append(
                    type(
                        "_LegacyRevisionResource",
                        (),
                        {
                            "path": item.get("path"),
                            "content": text.encode("utf-8"),
                            "mime_type": guess_mime_type(str(item.get("path"))),
                            "is_text": 1,
                        },
                    )()
                )

    current = await skill_repo.list_resources(db, skill.id)
    for row in current:
        await skill_repo.delete_resource(db, skill.id, row.path)
    for item in blobs:
        await skill_repo.put_resource(
            db,
            skill.id,
            item.path,
            item.content,
            item.mime_type or guess_mime_type(item.path),
            int(item.is_text),
        )
    # 回滚本身也是一次写入：版本号继续往前走，内容回到旧样子。
    skill = await skill_repo.update(
        db,
        code,
        {
            "body": revision.body or "",
            "content_hash": await _hash_for(db, skill, revision.body or ""),
            "status": "draft",
            "enabled": 0,
        },
        bump_version=True,
        tenant_id=tenant_id,
    )
    await _write_revision(
        db,
        skill,
        _operator_name(author),
        f"回滚到 v{payload.version}（草稿）",
    )
    used_by = await _used_by_map(db, tenant_id)
    return _to_out(skill, used_by.get(code, []))

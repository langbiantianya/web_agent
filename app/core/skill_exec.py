"""技能脚本的执行（设计文档 §22.8.3、§22.8.4）。

## 三道必须在 **web_agent 侧**做的校验

studio **没有技能的概念**——它收到的是「一段脚本正文 + 一个路径标识」（§22.8.3）。
所以下面这些校验它一个都做不了，只能留在这里：

1. **白名单**：脚本所在的技能必须在**本 Agent 绑定的技能**里——与
   :mod:`app.core.skill_reader` 同一套口径。不在就不执行。
2. **脚本路径格式**：复用 §22.7 的 :func:`~app.core.skill_paths.validate_resource_path`
   （``..`` 段、绝对路径、反斜杠全拒），并要求它在 ``scripts/`` 目录下。
3. **argv 形态**：参数里的 ``..`` 段、绝对路径、空字节全拒。

第 2、3 条必须在**请求发出去之前**做完——studio 收到的永远是已校验过的内容
（§22.13 的验收项：「请求根本没发出去 studio」）。

## 脚本正文随请求走

不共享卷：studio 保持无状态，web_agent 与 studio 之间没有卷依赖，两个容器可以
独立扩缩、独立升级（§22.8.3 的第一个字段决定）。代价是请求体变大——脚本通常
几 KB 到几十 KB，可接受。

## 输出不落库

脚本输出可能含敏感数据（§22.10）。它只作为 ``role=tool`` 的内容回灌给模型，
并截断到 ``tool_result_max_chars``。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.skill_paths import SkillPathError, validate_resource_path
from app.core.skill_runtime import (
    REQUIRED_ISOLATED_STUDIO_VERSION,
    SkillRuntimeError,
    runtime_from_skill,
)
from app.core.studio import (
    ExecResult,
    StudioError,
    get_studio_client,
    version_at_least,
)
from app.core.studio_context import current_exec_context, make_request_id

logger = logging.getLogger(__name__)

# 脚本必须放在这个目录下。它是 Agent Skills 的约定（scripts/ 放可执行脚本），
# 也是 §22.8.3 里的示例形状（scripts/extract.py）。
SCRIPT_DIR = "scripts/"

# 扩展名 → 语言。studio 侧的运行时矩阵是 python / bash / node（skill-service §10.1）。
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
}

# argv 的上限（skill-service §9.1）：64 项 / 单项 4 KB。
MAX_ARGV_ITEMS = 64
MAX_ARGV_ITEM_CHARS = 4096
# stdin 的上限（skill-service §9.1）：256 KB 文本。
MAX_STDIN_CHARS = 256 * 1024


class SkillExecError(Exception):
    """脚本执行前的校验失败——**请求根本没发出去**。"""


def guess_language(script: str) -> str:
    """按扩展名推断脚本语言。

    Args:
        script: 脚本相对路径。

    Returns:
        ``python`` / ``bash`` / ``node``。

    Raises:
        SkillExecError: 扩展名不认识。
    """
    _, _, suffix = script.rpartition(".")
    language = LANGUAGE_BY_SUFFIX.get(f".{suffix}".lower())
    if language is None:
        known = "、".join(sorted(LANGUAGE_BY_SUFFIX))
        raise SkillExecError(f"无法判断脚本「{script}」的语言（支持的扩展名：{known}）")
    return language


def validate_script_path(script: str) -> str:
    """校验脚本路径。

    Args:
        script: 脚本相对路径。

    Returns:
        规整后的路径。

    Raises:
        SkillExecError: 路径不合法，或不在 ``scripts/`` 目录下。
    """
    try:
        cleaned = validate_resource_path(script)
    except SkillPathError as exc:
        raise SkillExecError(str(exc)) from exc
    if not cleaned.startswith(SCRIPT_DIR):
        raise SkillExecError(f"脚本必须放在 {SCRIPT_DIR} 目录下")
    return cleaned


def validate_argv(argv: list[str] | None) -> list[str]:
    """校验传给脚本的参数。

    **参数绝不拼成 shell 字符串**——直接传给进程，参数里的 ``;`` ``|`` 就是普通
    字符（skill-service §9.1）。所以这里校验的不是「shell 安全」，而是**路径
    形态**：``..`` 段与绝对路径都不该出现在参数里。

    Args:
        argv: 模型给的参数数组。

    Returns:
        规整后的参数数组。

    Raises:
        SkillExecError: 参数形态不合法。
    """
    if argv is None:
        return []
    if not isinstance(argv, list):
        raise SkillExecError("args 必须是字符串数组")
    if len(argv) > MAX_ARGV_ITEMS:
        raise SkillExecError(f"参数超过 {MAX_ARGV_ITEMS} 项")
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise SkillExecError("args 里只能放字符串")
        if len(item) > MAX_ARGV_ITEM_CHARS:
            raise SkillExecError(f"单个参数超过 {MAX_ARGV_ITEM_CHARS} 字符")
        if "\x00" in item:
            raise SkillExecError("参数不能包含空字节")
        if "\\" in item:
            raise SkillExecError("参数不能包含反斜杠")
        # --flag=value 这种形态只看值那一段。
        _, sep, tail = item.partition("=")
        candidate = tail if sep else item
        if candidate.startswith("/"):
            raise SkillExecError("参数里的路径必须是相对路径，不能以 / 开头")
        if any(seg == ".." for seg in candidate.split("/")):
            raise SkillExecError("参数里的路径不能包含 ..")
        out.append(item)
    return out


async def _load_script(
    db: AsyncSession, code: str, path: str, max_chars: int
) -> tuple[str, Any]:
    """从数据库里读出脚本正文与技能行。

    返回技能行是为了顺带拿到它的运行档声明（``metadata`` 里的
    ``runtime-profile`` / ``runtime-image``），不必再查一次库。

    Args:
        db: 数据库会话。
        code: 技能 code。
        path: 脚本相对路径（已规整）。
        max_chars: 正文字符上限。

    Returns:
        ``(脚本正文, 技能行)``。

    Raises:
        SkillExecError: 技能不存在、没有这个资源、资源不是文本、超长。
    """
    from app.repo.skill_repo import skill_repo

    skill = await skill_repo.get_by_code(db, code)
    if skill is None:
        raise SkillExecError(f"技能「{code}」不存在。")
    row = await skill_repo.get_resource(db, skill.id, path)
    if row is None:
        raise SkillExecError(f"技能「{code}」里没有资源「{path}」。")
    if not row.is_text:
        raise SkillExecError(
            f"资源「{path}」是二进制文件（{row.mime_type}），不能当脚本执行。"
        )
    content = row.content.decode("utf-8", errors="replace")
    if len(content) > max_chars:
        raise SkillExecError(
            f"脚本正文超过 {max_chars} 字符上限（当前 {len(content)} 字符）。"
        )
    return content, skill


async def read_script(
    db: AsyncSession, code: str, path: str, max_chars: int
) -> str:
    """从数据库里读出脚本正文。

    Args:
        db: 数据库会话。
        code: 技能 code。
        path: 脚本相对路径（已规整）。
        max_chars: 正文字符上限。

    Returns:
        脚本正文。

    Raises:
        SkillExecError: 技能不存在、没有这个资源、资源不是文本、超长。
    """
    content, _skill = await _load_script(db, code, path, max_chars)
    return content


def format_exec_result(result: ExecResult) -> str:
    """把执行结果拼成给模型看的文本。

    形状取自 §22.8.4 的例子：模型要能据此判断成功与否——退出码、stdout、stderr
    都要在。

    Args:
        result: 执行结果。

    Returns:
        给模型看的文本。
    """
    lines: list[str] = []
    if result.status != "completed":
        lines.append(f"status: {result.status}")
    if result.profile and result.profile != "fast":
        lines.append(f"profile: {result.profile}")
        lines.append(f"startup_ms: {result.startup_ms}")
    lines.append(f"exit_code: {result.exit_code}")
    if result.error_code:
        lines.append(f"error_code: {result.error_code}")
    if result.error:
        lines.append(f"error: {result.error}")
    lines.append("--- stdout ---")
    lines.append(result.stdout)
    lines.append("--- stderr ---")
    lines.append(result.stderr)
    if result.truncated:
        lines.append("（输出超过上限，已截断）")
    return "\n".join(lines)


async def exec_skill_script(
    db: AsyncSession,
    *,
    skill: str,
    script: str,
    argv: list[str] | None = None,
    stdin: str = "",
    timeout_ms: int | None = None,
    allowed_codes: set[str] | None = None,
) -> tuple[bool, str]:
    """执行一个技能脚本。

    Args:
        db: 数据库会话。
        skill: 技能 code。
        script: 脚本相对路径。
        argv: 传给脚本的参数。
        stdin: 标准输入（文本）。
        timeout_ms: 执行时限（毫秒）；超过上限会被服务端压到上限。
        allowed_codes: 本 Agent 绑定的技能 code 集合；``None`` 表示不限制。

    Returns:
        ``(ok, text)``。``ok`` 为 False 时 ``text`` 是给模型看的错误说明。
    """
    client = get_studio_client()
    if client is None:
        return False, "错误：脚本执行未启用（studio 没有配置）。"

    # 先看授权再看存在性：不该跑的技能，连「它在不在」都不该告诉模型。
    if allowed_codes is not None and skill not in allowed_codes:
        return False, f"错误：技能「{skill}」不在本 Agent 的授权列表里，不能执行。"

    try:
        cleaned_path = validate_script_path(script)
        language = guess_language(cleaned_path)
        clean_argv = validate_argv(argv)
        if len(stdin) > MAX_STDIN_CHARS:
            raise SkillExecError(f"stdin 超过 {MAX_STDIN_CHARS} 字符上限")
        content, skill_row = await _load_script(
            db, skill, cleaned_path, settings.studio_exec_max_script_chars
        )
        runtime = runtime_from_skill(skill_row)
    except (SkillExecError, SkillRuntimeError) as exc:
        return False, f"错误：{exc}"

    health = client.health_info
    if runtime.is_isolated:
        # 档位能力按 TTL 刷新：studio 滚动升级 / 镜像变化之后，启动时那份
        # 能力快照可能已经过时。
        ensure = getattr(client, "ensure_health", None)
        if ensure is not None:
            health = await ensure()
        if health is None or not health.supports_isolated:
            return False, (
                f"错误：技能「{skill}」声明了强隔离执行（isolated 档），"
                "但 studio 没有可用的沙箱运行时。请让运维部署调度器，"
                "或把该技能的 runtime-profile 改回 fast。"
            )
        if not version_at_least(health.version, REQUIRED_ISOLATED_STUDIO_VERSION):
            return False, (
                f"错误：技能「{skill}」需要 studio >= "
                f"{REQUIRED_ISOLATED_STUDIO_VERSION}（当前 {health.version}）。"
            )
        if runtime.image and health.image(runtime.image) is None:
            return False, (
                f"错误：技能「{skill}」声明的镜像「{runtime.image}」"
                "不在 studio 的镜像目录里。"
            )

    # studio 报出来的运行时才是它真能跑的（skill-service §10.2）。isolated 技能
    # 以目标镜像声明的语言为准——它和 fast 镜像的运行时集可能不同。
    allowed_languages = health.languages if health is not None else ()
    if runtime.is_isolated and health is not None and runtime.image:
        image = health.image(runtime.image)
        if image is not None and image.languages:
            allowed_languages = image.languages
    if health is not None and allowed_languages and language not in allowed_languages:
        return False, (
            f"错误：studio 不支持 {language} 运行时"
            f"（当前支持：{'、'.join(allowed_languages)}）。"
        )

    ctx = current_exec_context()
    request_id = make_request_id(skill, cleaned_path, clean_argv)
    timeout = runtime.timeout_ms if runtime.timeout_ms is not None else timeout_ms

    try:
        result = await client.exec_script(
            skill=skill,
            script=cleaned_path,
            language=language,
            content=content,
            argv=clean_argv,
            stdin=stdin,
            timeout_ms=timeout,
            profile=runtime.profile,
            image=runtime.image,
            run_id=ctx.run_id if ctx else "",
            node_id=ctx.node_id if ctx else "",
            request_id=request_id,
            tenant_id=ctx.tenant_id if ctx else "default",
        )
    except StudioError as exc:
        # studio 不可用 / 超时 / 参数被拒——都是**服务级**问题，回灌给模型，
        # 不失败节点（§17.5）。
        logger.warning("studio 执行失败：%s", exc)
        # 能力类失败说明启动时那份快照可能已经过时（studio 滚动升级、镜像变化）。
        # 把时间戳清掉，下一次 isolated 调用会强制重新探活。
        if exc.code in ("capability_missing", "image_unknown"):
            try:
                client.health_fetched_at = None
            except AttributeError:
                pass
        return False, f"错误：{exc}"

    # 最后一道对账：请求了 isolated，结果却不是 isolated。真正的 StudioClient
    # 已经在响应处拦过（StudioDowngradeError）；这里再拦一次，是为了让
    # 「不管客户端怎么实现，降级结果都不会被当成成功」这件事留在消费点上。
    if runtime.is_isolated and result.profile != "isolated":
        return False, (
            f"错误：studio 实际以 {result.profile or '未知'} 档执行了要求 isolated "
            "档的脚本，已拒绝该结果。"
        )

    return result.ok, format_exec_result(result)

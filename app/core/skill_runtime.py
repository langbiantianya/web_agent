"""技能运行档位的声明、校验与启动期审计（skill-service §2.4、§14.3）。

## 为什么档位要写在技能里

`STUDIO_ISOLATED_SKILL_CODES` 是**部署侧**的一份清单，它是 fail-open 的：
技能改名、漏配、某个副本配置漂移，都会让技能**静默**落回 `fast`。而「这个脚本
需要第三方依赖 / 需要强隔离」是随技能走的事实，不是某个环境的记忆。

所以 v1 用 SKILL.md 的 ``metadata`` 声明（扁平键，因为
:func:`app.core.skill_pack.parse_skill_md` 会把 metadata 的值强制成字符串）：

```yaml
metadata:
  runtime-profile: isolated
  runtime-image: ml-py311
  runtime-timeout-ms: "60000"
```

## 为什么只升不降

请求档位由调用方按技能声明算出来；服务端可以因为运维覆盖把它**升级**成
isolated，但绝不能把 isolated 降成 fast——降级且没有痕迹是最坏的一种失败。
调用方还会拿响应里的 ``profile`` 再对账一次（:mod:`app.core.studio`）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PROFILE_FAST = "fast"
PROFILE_ISOLATED = "isolated"

# SKILL.md metadata 里的键名。用扁平键而不是嵌套对象：见模块开头。
KEY_PROFILE = "runtime-profile"
KEY_IMAGE = "runtime-image"
KEY_TIMEOUT = "runtime-timeout-ms"

VALID_PROFILES = frozenset({PROFILE_FAST, PROFILE_ISOLATED})

# 单技能声明的时限范围。上限刻意比 studio 的 ``max_timeout_ms`` 宽：
# 部署可能调大 studio 的上限，客户端侧的 ``effective_timeout_ms`` 还会再压一道。
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 600_000

# 请求带档位 / 响应回显档位是 studio 2.0.0 起的语义。声明 isolated 的技能
# 必须落在这个版本之上，否则老 studio 会忽略 profile 字段（静默降级）。
REQUIRED_ISOLATED_STUDIO_VERSION = "2.0.0"


class SkillRuntimeError(ValueError):
    """技能声明的运行档位不合法——导入/保存期就拒。"""


@dataclass(frozen=True, slots=True)
class SkillRuntime:
    """一个技能声明的运行方式。"""

    profile: str = PROFILE_FAST
    image: str = ""
    timeout_ms: int | None = None

    @property
    def is_isolated(self) -> bool:
        """是否声明了强隔离。"""
        return self.profile == PROFILE_ISOLATED

    def describe(self) -> str:
        """给日志/错误信息用的一句话。"""
        parts = [f"profile={self.profile}"]
        if self.image:
            parts.append(f"image={self.image}")
        if self.timeout_ms is not None:
            parts.append(f"timeout_ms={self.timeout_ms}")
        return "，".join(parts)


def parse_skill_runtime(metadata: Mapping[str, Any] | None) -> SkillRuntime:
    """从 SKILL.md 的 metadata 解析运行档位。

    Args:
        metadata: 技能 metadata（可能是 None / 空）。

    Returns:
        :class:`SkillRuntime`；没有声明时是 ``fast``。

    Raises:
        SkillRuntimeError: 档位名不认识、镜像与档位矛盾、时限不是合法整数或超范围。
    """
    data = metadata or {}
    profile = str(data.get(KEY_PROFILE, PROFILE_FAST) or PROFILE_FAST).strip().lower()
    if profile not in VALID_PROFILES:
        raise SkillRuntimeError(
            f"{KEY_PROFILE} 只能是 fast 或 isolated（当前 {profile!r}）"
        )
    image = str(data.get(KEY_IMAGE, "") or "").strip()
    if image and profile != PROFILE_ISOLATED:
        raise SkillRuntimeError(
            f"{KEY_IMAGE} 只在 {PROFILE_ISOLATED} 档有意义——"
            f"当前 {KEY_PROFILE}={profile}"
        )
    raw_timeout = data.get(KEY_TIMEOUT)
    timeout_ms: int | None = None
    if raw_timeout not in (None, ""):
        try:
            timeout_ms = int(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise SkillRuntimeError(f"{KEY_TIMEOUT} 必须是整数毫秒") from exc
        if not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS:
            raise SkillRuntimeError(
                f"{KEY_TIMEOUT} 必须在 {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS} 毫秒之间"
            )
    return SkillRuntime(profile=profile, image=image, timeout_ms=timeout_ms)


def runtime_from_skill(skill: Any) -> SkillRuntime:
    """从 ORM ``Skill`` 行读运行档位。

    Args:
        skill: ``app.models.skill.Skill`` 实例。

    Returns:
        :class:`SkillRuntime`。

    Raises:
        SkillRuntimeError: 库里存了不合法的声明（历史数据可能脏）。
    """
    return parse_skill_runtime(getattr(skill, "metadata_", None))


async def audit_skill_runtime_bindings(
    db: Any,
    health: Any,
    *,
    required: bool = True,
) -> list[tuple[str, str]]:
    """启动期对账：被 Agent 绑定的技能，在本部署能不能执行。

    为什么要在启动时做：技能绑定是配置，配置错了应该在部署时暴露。等模型第一
    次调用才发现，现场是「这个 Agent 好像少了个能力」，排查成本高得多。

    只检查**已启用 Agent 绑定的、已启用的**技能——没绑定的技能暂时跑不了不
    影响任何 run，没必要拦启动。

    Args:
        db: 数据库会话（调用方开）。
        health: :class:`app.core.studio.StudioHealth`。
        required: 为 True 时发现问题**直接抛** ``StudioStartupError``。

    Returns:
        ``[(skill_code, reason), ...]``；没有问题时是空列表。

    Raises:
        StudioStartupError: ``required=True`` 且存在不可执行的技能。
    """
    from app.core.studio import StudioStartupError, version_at_least
    from app.repo.agent_repo import agent_repo
    from app.repo.skill_repo import skill_repo

    agents = await agent_repo.list(db, only_enabled=True, limit=1000)
    codes: list[str] = []
    for agent in agents:
        raw = getattr(agent, "skills", None)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str) and item.strip() and item.strip() not in codes:
                codes.append(item.strip())
    if not codes:
        return []
    skills = await skill_repo.list_by_codes(db, codes)

    problems: list[tuple[str, str]] = []
    for skill in skills:
        if not getattr(skill, "enabled", 0):
            continue
        try:
            runtime = runtime_from_skill(skill)
        except SkillRuntimeError as exc:
            problems.append((skill.code, f"运行档声明非法：{exc}"))
            continue
        if not runtime.is_isolated:
            continue
        if not getattr(health, "supports_isolated", False):
            problems.append((skill.code, "需要 isolated 档，但 studio 没有可用的沙箱运行时"))
            continue
        if not version_at_least(
            getattr(health, "version", ""), REQUIRED_ISOLATED_STUDIO_VERSION
        ):
            problems.append(
                (
                    skill.code,
                    f"需要 studio >= {REQUIRED_ISOLATED_STUDIO_VERSION}"
                    f"（当前 {getattr(health, 'version', '未知')}）",
                )
            )
            continue
        if runtime.image and health.image(runtime.image) is None:
            problems.append(
                (skill.code, f"镜像「{runtime.image}」不在 studio 的镜像目录里")
            )

    if problems:
        message = "；".join(f"{code}：{reason}" for code, reason in problems)
        if required:
            raise StudioStartupError(f"以下技能在本部署不可执行：{message}")
        logger.warning("以下技能在本部署不可执行：%s", message)
    return problems

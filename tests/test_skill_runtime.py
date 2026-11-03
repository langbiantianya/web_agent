"""技能运行档的声明、校验与启动期审计（skill-service §2.4、§14.3）。"""

from __future__ import annotations

import itertools

import pytest

from app.core.skill_runtime import (
    SkillRuntimeError,
    audit_skill_runtime_bindings,
    parse_skill_runtime,
    runtime_from_skill,
)
from app.core.studio import (
    StudioHealth,
    StudioImage,
    StudioProfile,
    StudioStartupError,
)
from app.repo.agent_repo import agent_repo
from app.repo.skill_repo import skill_repo

_counter = itertools.count()


def _code(prefix: str = "rt") -> str:
    """造一个不重复的技能/Agent code。"""
    return f"{prefix}_{next(_counter)}"


def _health(
    *,
    isolated_ready: bool | None = True,
    version: str = "2.0.0",
    images: tuple[StudioImage, ...] = (),
) -> StudioHealth:
    """造一份能力快照；``isolated_ready=None`` 表示老 studio（没有 profiles）。"""
    profiles = (
        ()
        if isolated_ready is None
        else (
            StudioProfile(name="fast", ready=True, max_concurrency=4),
            StudioProfile(name="isolated", ready=isolated_ready, max_concurrency=2),
        )
    )
    return StudioHealth(
        ready=True,
        version=version,
        languages=("python",),
        max_concurrency=4,
        profile="fast",
        profiles=profiles,
        images=images,
    )


# --------------------------------------------------------------- 解析


def test_no_declaration_means_fast():
    """没声明就是 fast——老技能一行都不用改。"""
    assert parse_skill_runtime(None).profile == "fast"
    assert parse_skill_runtime({}).is_isolated is False


def test_parse_isolated_with_image_and_timeout():
    """声明 isolated + 镜像 + 时限。"""
    runtime = parse_skill_runtime(
        {
            "runtime-profile": "isolated",
            "runtime-image": "ml-py311",
            "runtime-timeout-ms": "60000",
        }
    )
    assert runtime.is_isolated is True
    assert runtime.image == "ml-py311"
    assert runtime.timeout_ms == 60_000


def test_profile_is_case_insensitive():
    """大小写不该让人踩坑。"""
    assert parse_skill_runtime({"runtime-profile": "ISOLATED"}).profile == "isolated"


def test_unknown_profile_rejected():
    """档位名不认识 → 拒绝，不猜。"""
    with pytest.raises(SkillRuntimeError):
        parse_skill_runtime({"runtime-profile": "gpu"})


def test_image_with_fast_rejected():
    """镜像只在 isolated 档有意义——fast 声明镜像多半是写错了。"""
    with pytest.raises(SkillRuntimeError):
        parse_skill_runtime(
            {"runtime-profile": "fast", "runtime-image": "ml-py311"}
        )


@pytest.mark.parametrize("bad", ["soon", 0, -1, 600_001])
def test_bad_timeout_rejected(bad):
    """时限必须是合法范围内的整数毫秒。"""
    with pytest.raises(SkillRuntimeError):
        parse_skill_runtime(
            {"runtime-profile": "isolated", "runtime-timeout-ms": bad}
        )


def test_runtime_from_skill_row():
    """从 ORM 行读 metadata。"""

    class _Row:
        metadata_ = {"runtime-profile": "isolated", "runtime-image": "ml"}

    assert runtime_from_skill(_Row()).image == "ml"


# --------------------------------------------------------------- 启动审计


async def _make_skill(db, code: str, **metadata):
    """建一个技能，metadata 可选。"""
    return await skill_repo.create(
        db,
        code=code,
        name=code,
        description="d",
        body="b",
        content_hash="h",
        metadata_=metadata or None,
        enabled=1,
    )


async def _bind(db, skills: list[str]) -> None:
    """建一个启用的 Agent 绑定这些技能。"""
    await agent_repo.create(
        db,
        code=_code("ag"),
        name="n",
        system_prompt="x",
        model="m",
        base_url="http://x",
        api_key_cipher="c",
        skills=skills,
        enabled=1,
    )


async def test_audit_flags_isolated_skill_without_runtime(db):
    """声明 isolated 但 studio 没有沙箱运行时 → 启动期就报错。"""
    code = _code()
    await _make_skill(db, code, **{"runtime-profile": "isolated"})
    await _bind(db, [code])
    health = _health(isolated_ready=False)
    with pytest.raises(StudioStartupError) as ei:
        await audit_skill_runtime_bindings(db, health, required=True)
    assert code in str(ei.value)


async def test_audit_is_warning_only_when_not_required(db):
    """required=false 时只记问题、不抛。"""
    code = _code()
    await _make_skill(db, code, **{"runtime-profile": "isolated"})
    await _bind(db, [code])
    problems = await audit_skill_runtime_bindings(
        db, _health(isolated_ready=False), required=False
    )
    assert [item[0] for item in problems if item[0] == code] == [code]


async def test_audit_accepts_ready_runtime_and_known_image(db):
    """运行时就绪、镜像在目录里 → 没问题。"""
    code = _code()
    await _make_skill(
        db,
        code,
        **{"runtime-profile": "isolated", "runtime-image": "ml"},
    )
    await _bind(db, [code])
    problems = await audit_skill_runtime_bindings(
        db,
        _health(images=(StudioImage(name="ml", languages=("python",)),)),
        required=True,
    )
    assert [item for item in problems if item[0] == code] == []


async def test_audit_flags_unknown_image(db):
    """镜像名不在目录里 → 该技能配置期不可用。"""
    code = _code()
    await _make_skill(
        db,
        code,
        **{"runtime-profile": "isolated", "runtime-image": "missing"},
    )
    await _bind(db, [code])
    problems = await audit_skill_runtime_bindings(
        db, _health(), required=False
    )
    current = [item for item in problems if item[0] == code]
    assert current and "missing" in current[0][1]


async def test_audit_flags_old_studio_without_profiles(db):
    """老 studio 没有 profiles → 视为 fast-only，声明 isolated 的技能不可执行。"""
    code = _code()
    await _make_skill(db, code, **{"runtime-profile": "isolated"})
    await _bind(db, [code])
    problems = await audit_skill_runtime_bindings(
        db, _health(isolated_ready=None), required=False
    )
    current = [item for item in problems if item[0] == code]
    assert current and "isolated" in current[0][1]

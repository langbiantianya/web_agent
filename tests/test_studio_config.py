"""studio 配置校验（skill-service §2.4、§14.3）。"""

from __future__ import annotations

import pytest

from services.studio.config import StudioConfig, StudioConfigError


def test_default_version_is_2_0_0():
    """2.0.0 是「请求带档位、响应回显」的语义版本。"""
    assert StudioConfig().version == "2.0.0"


def test_unknown_profile_is_rejected():
    """档位名只能是 fast / isolated。"""
    with pytest.raises(StudioConfigError):
        StudioConfig(profile="gpu").validate()


def test_isolated_codes_without_dispatcher_is_rejected():
    """配了 isolated 技能却没有调度器 → 启动失败，而不是每次调用报错。"""
    with pytest.raises(StudioConfigError) as ei:
        StudioConfig(isolated_skill_codes=("demo",)).validate()
    assert "DISPATCHER" in str(ei.value)


def test_isolated_images_without_dispatcher_is_rejected():
    """配了镜像目录却没有调度器 → 同样启动失败。"""
    with pytest.raises(StudioConfigError):
        StudioConfig(
            isolated_images=(("ml", "registry/x@sha256:abc"),)
        ).validate()


def test_valid_isolated_config_passes():
    """有调度器、有镜像 → 通过；按目录名能取到完整 ref。"""
    cfg = StudioConfig(
        isolated_skill_codes=("demo",),
        isolated_dispatcher_addr="studio-dispatcher:9100",
        isolated_images=(("ml", "registry/x@sha256:abc"),),
    )
    cfg.validate()
    assert cfg.image_ref("ml") == "registry/x@sha256:abc"
    assert cfg.image_ref("nope") is None


def test_from_env_parses_image_directory(monkeypatch):
    """环境变量里的 ``目录名=ref`` 要解析成对。"""
    monkeypatch.setenv("STUDIO_ISOLATED_DISPATCHER_ADDR", "dispatcher:9100")
    monkeypatch.setenv(
        "STUDIO_ISOLATED_IMAGES",
        "ml=registry/x@sha256:abc, default=registry/y@sha256:def",
    )
    monkeypatch.setenv("STUDIO_ISOLATED_STARTUP_BUDGET_MS", "12345")
    cfg = StudioConfig.from_env()
    cfg.validate()
    assert cfg.image_ref("ml") == "registry/x@sha256:abc"
    assert cfg.image_ref("default") == "registry/y@sha256:def"
    assert cfg.isolated_startup_budget_ms == 12345

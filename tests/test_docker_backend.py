"""Docker 后端的固定模板与结果映射（不起真 Docker）。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from services.dispatcher.backend import SandboxError
from services.dispatcher.config import DispatcherConfig
from services.dispatcher import docker_backend as docker_backend_module
from services.dispatcher.docker_backend import DockerBackend
from services.sandbox.protocol import SandboxSpec, encode_frames


class _FakeContainer:
    """假的 docker 容器。"""

    def __init__(self) -> None:
        """初始化状态。"""
        self.id = "container-123456789"
        self.attrs = {"State": {"ExitCode": 0, "OOMKilled": False}}
        self.payload: dict = {}
        self.started = False
        self.removed = False
        self.created_kwargs: dict = {}

    def start(self) -> None:
        """标记已启动。"""
        self.started = True

    def wait(self, timeout: float | None = None) -> dict:
        """记下等待时限。"""
        self.wait_timeout = timeout
        return {"StatusCode": 0}

    def logs(self, stdout: bool = True, stderr: bool = False) -> bytes:
        """返回一段控制帧。"""
        return encode_frames(
            nonce=self.payload["nonce"],
            exit_code=0,
            truncated=False,
            stdout=b"hello\n",
            stderr=b"warn",
            duration_ms=5,
        ).encode("utf-8")

    def remove(self, force: bool = False) -> None:
        """标记已删除。"""
        self.removed = True


class _FakeContainers:
    """假的 containers 集合。"""

    def __init__(self, container: _FakeContainer) -> None:
        """记住容器。"""
        self.container = container
        self.create_kwargs: dict = {}

    def create(self, image: str, **kwargs) -> _FakeContainer:
        """记下创建参数，并从只读挂载里读出 payload。"""
        self.create_kwargs = {"image": image, **kwargs}
        host_dir = next(iter(kwargs["volumes"]))
        self.container.payload = json.loads(
            (Path(host_dir) / "payload.json").read_bytes()
        )
        return self.container


class _FakeDocker:
    """假的 DockerClient。"""

    def __init__(self, container: _FakeContainer) -> None:
        """建 containers。"""
        self.containers = _FakeContainers(container)
        self.images = _FakeImages()

    def ping(self) -> bool:
        """假的探活。"""
        return True


class _FakeImages:
    """记录预热调用。"""

    def __init__(self) -> None:
        self.gets: list[str] = []
        self.pulls: list[str] = []

    def get(self, ref: str) -> object:
        """默认所有目录镜像都已存在。"""
        self.gets.append(ref)
        return object()

    def pull(self, ref: str) -> object:
        """模拟拉取。"""
        self.pulls.append(ref)
        return object()


def _backend(
    container: _FakeContainer, payload_dir: Path
) -> tuple[DockerBackend, _FakeDocker]:
    """造一个后端，把 Docker 客户端换成假的。"""
    profile = payload_dir.parent / "seccomp-profile.json"
    profile.write_text(
        json.dumps(
            {
                "defaultAction": "SCMP_ACT_ALLOW",
                "syscalls": [
                    {
                        "names": ["ptrace"],
                        "action": "SCMP_ACT_ERRNO",
                        "errnoRet": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    backend = DockerBackend(
        DispatcherConfig(
            images=(
                (
                    "default",
                    "registry/sandbox@sha256:" + "a" * 64,
                ),
            ),
            payload_dir=str(payload_dir),
            seccomp_profile=str(profile),
        )
    )
    fake = _FakeDocker(container)
    backend._docker = lambda: fake  # noqa: SLF001
    return backend, fake


def _spec(**overrides) -> SandboxSpec:
    """一个合法的执行输入。"""
    params = {
        "skill": "demo",
        "script": "scripts/a.py",
        "language": "python",
        "content": "print(1)",
        "timeout_ms": 1000,
        "image": "default",
        "request_id": "req-1",
    }
    params.update(overrides)
    return SandboxSpec(**params)


def test_container_template_is_fixed_and_hardened(tmp_path):
    """容器参数来自模板：无网络、只读 rootfs、非 root、drop caps。"""
    container = _FakeContainer()
    backend, fake = _backend(container, tmp_path)
    backend._run_sync(_spec())  # noqa: SLF001
    kwargs = fake.containers.create_kwargs
    assert kwargs["image"] == "registry/sandbox@sha256:" + "a" * 64
    assert kwargs["network_disabled"] is True
    assert kwargs["read_only"] is True
    assert kwargs["user"] == "1000:1000"
    # 镜像把 services 包放在 /app；从 /tmp 启动会让 ``python -m`` 找不到
    # services.sandbox.runner，脚本根本进不了 runner。
    assert kwargs["working_dir"] == "/app"
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges" in kwargs["security_opt"]
    assert any(item.startswith("seccomp=") for item in kwargs["security_opt"])
    assert kwargs["pids_limit"] == 256
    assert kwargs["mem_limit"] == "512m"
    assert "SECRET" not in json.dumps(kwargs["environment"])
    host_dir, mount = next(iter(kwargs["volumes"].items()))
    assert mount["bind"] == "/run/sandbox"
    assert mount["mode"] == "ro"
    assert container.started is True
    assert container.removed is True


async def test_prepare_materializes_profile_and_prewarms_images(tmp_path):
    """Docker daemon 可见目录里要有 profile，镜像目录要在启动期预热。"""
    container = _FakeContainer()
    backend, fake = _backend(container, tmp_path)
    await backend.prepare()
    profiles = list(tmp_path.glob(".webagent-seccomp-*.json"))
    assert len(profiles) == 1
    assert json.loads(profiles[0].read_text(encoding="utf-8"))[
        "defaultAction"
    ] == "SCMP_ACT_ALLOW"
    assert fake.images.gets == ["registry/sandbox@sha256:" + "a" * 64]
    assert fake.images.pulls == []


def test_missing_seccomp_profile_fails_closed(tmp_path):
    """生产默认要求自定义 profile；文件缺失时不能静默退回默认 profile。"""
    backend = DockerBackend(
        DispatcherConfig(
            images=(("default", "registry/sandbox@sha256:" + "a" * 64),),
            payload_dir=str(tmp_path),
            seccomp_profile=str(tmp_path / "missing.json"),
        )
    )
    backend._docker = lambda: _FakeDocker(_FakeContainer())  # noqa: SLF001
    with pytest.raises(SandboxError) as excinfo:
        backend._prepare_sync()  # noqa: SLF001
    assert excinfo.value.code == "sandbox_unavailable"


@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX 文件权限")
def test_payload_is_readable_by_sandbox_uid(tmp_path, monkeypatch):
    """执行目录可遍历、payload 只读——沙箱以 uid 1000 运行。"""
    exec_dir = tmp_path / "sbx-fixed"

    def _mkdtemp(**_kwargs):
        """用固定路径模拟 tempfile.mkdtemp，便于检查目录权限。"""
        exec_dir.mkdir()
        return str(exec_dir)

    monkeypatch.setattr(docker_backend_module.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(docker_backend_module.shutil, "rmtree", lambda *_a, **_k: None)
    backend, _ = _backend(_FakeContainer(), tmp_path)
    backend._run_sync(_spec())  # noqa: SLF001
    assert stat.S_IMODE(exec_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((exec_dir / "payload.json").stat().st_mode) == 0o644


def test_result_is_parsed_from_frames(tmp_path):
    """控制帧 → SandboxResult。"""
    container = _FakeContainer()
    backend, _ = _backend(container, tmp_path)
    result = backend._run_sync(_spec())  # noqa: SLF001
    assert result.status == "completed"
    assert result.stdout == "hello\n"
    assert result.stderr == "warn"
    assert result.truncated is False
    assert result.sandbox_id == "container-12"


def test_oom_is_limited(tmp_path):
    """OOMKilled → LIMITED + sandbox_oom。"""
    container = _FakeContainer()
    container.attrs = {"State": {"ExitCode": 137, "OOMKilled": True}}
    backend, _ = _backend(container, tmp_path)
    result = backend._run_sync(_spec())  # noqa: SLF001
    assert result.status == "limited"
    assert result.error_code == "sandbox_oom"
    assert "内存" in result.error


def test_missing_frames_is_result_lost(tmp_path):
    """没有控制帧 → FAILED + sandbox_result_lost。"""
    container = _FakeContainer()
    container.logs = lambda stdout=True, stderr=False: b"not a frame"  # noqa: ARG005
    backend, _ = _backend(container, tmp_path)
    result = backend._run_sync(_spec())  # noqa: SLF001
    assert result.status == "failed"
    assert result.error_code == "sandbox_result_lost"


def test_unknown_image_raises(tmp_path):
    """目录里没有的镜像 → SandboxError(image_unknown)，不创建容器。"""
    container = _FakeContainer()
    backend, fake = _backend(container, tmp_path)
    with pytest.raises(SandboxError) as ei:
        backend._run_sync(_spec(image="missing"))  # noqa: SLF001
    assert ei.value.code == "image_unknown"
    assert fake.containers.create_kwargs == {}

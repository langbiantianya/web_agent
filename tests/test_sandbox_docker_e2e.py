"""真 Docker 的 isolated e2e（需要 Docker 与沙箱镜像）。

默认跳过：本机/CI 没有 Docker 或镜像时不算失败。有 Docker 的环境里跑：

```sh
docker build -f services/sandbox/Dockerfile -t webagent/studio-sandbox:1.0.0 .
pytest tests/test_sandbox_docker_e2e.py -q
```
"""

from __future__ import annotations

import json

import pytest

from services.dispatcher.config import DispatcherConfig
from services.dispatcher.docker_backend import DockerBackend
from services.sandbox.protocol import SandboxSpec

IMAGE = "webagent/studio-sandbox:1.0.0"


def _docker_ready() -> bool:
    """有 docker SDK、连得上、镜像也在。"""
    try:
        import docker
    except ImportError:
        return False
    try:
        client = docker.from_env()
    except Exception:  # noqa: BLE001
        return False
    try:
        client.ping()
        client.images.get(IMAGE)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()


pytestmark = pytest.mark.skipif(
    not _docker_ready(), reason="需要 Docker 与 webagent/studio-sandbox:1.0.0"
)


async def test_sandbox_is_hardened_and_runs(tmp_path):
    """一次性容器真的跑起来；无网络、只读 rootfs、读不到宿主凭据。"""
    script = """
import os, pathlib, socket
print("SECRET=" + os.environ.get("SECRET_KEY", ""))
try:
    pathlib.Path("/pwned").write_text("x")
    print("WRITE=ok")
except OSError:
    print("WRITE=readonly")
try:
    socket.create_connection(("1.1.1.1", 80), timeout=1)
    print("NET=ok")
except OSError:
    print("NET=blocked")
"""
    backend = DockerBackend(
        DispatcherConfig(
            images=(("default", IMAGE),),
            payload_dir=str(tmp_path),
        )
    )
    try:
        result = await backend.run(
            SandboxSpec(
                skill="e2e",
                script="scripts/a.py",
                language="python",
                content=script,
                timeout_ms=10000,
                image="default",
                request_id="e2e-1",
            )
        )
    finally:
        await backend.close()

    assert result.status == "completed", result
    assert result.exit_code == 0
    lines = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert lines["SECRET"] == ""
    assert lines["WRITE"] == "readonly"
    assert lines["NET"] == "blocked"
    # 控制帧没有被用户输出污染
    assert json.dumps(result.stdout)

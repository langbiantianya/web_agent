"""proto 契约的一致性（主设计 §22.8.3、skill-service §5）。

契约散在**三处**：proto 文件、主设计文档、skill-service 文档。任何一处漏改，
调用方与服务方就会拿着不同的契约说话——而且**不会报错**，只是字段对不上或
被静默忽略。所以这里把「三处必须逐字一致」钉成测试。

还钉住一件更要紧的事：**桩代码能 import**。生成物如果因为目录结构或
``__init__.py`` 缺失而 import 不了，契约再一致也没用——那时是运行期才发现。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTO_FILE = ROOT / "proto" / "webagent" / "studio" / "v1" / "studio.proto"
BLOCK = re.compile(r"```proto\n(.*?)```", re.DOTALL)

DOCS = [
    ("app/design/v4-tools-skills.md", "主设计 V4 §22.8.3"),
    ("services/studio/design/v4-current.md", "studio V4 §5"),
]


def _extract(path: pathlib.Path) -> str | None:
    """抽出文档里的 proto 围栏块。"""
    match = BLOCK.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def test_proto_file_exists():
    """契约文件在仓库里，只有一份。"""
    assert PROTO_FILE.exists(), f"缺少契约文件：{PROTO_FILE}"


@pytest.mark.parametrize("rel,label", DOCS)
def test_doc_proto_block_matches_file(rel: str, label: str):
    """两份文档里贴的 proto 片段与契约文件**逐字一致**。"""
    block = _extract(ROOT / rel)
    assert block is not None, f"{label}：{rel} 里没有 ```proto 围栏块"
    assert block == PROTO_FILE.read_text(encoding="utf-8"), f"{label} 与契约文件不一致"


def test_both_docs_agree_with_each_other():
    """两份文档之间也一致（少一处修改时这里能更早发现）。"""
    a = _extract(ROOT / "app" / "design" / "v4-tools-skills.md")
    b = _extract(ROOT / "services" / "studio" / "design" / "v4-current.md")
    assert a is not None and b is not None
    assert a == b


def test_check_script_passes():
    """一致性检查脚本本身跑得通（它要进 CI）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_proto_contract.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_check_script_detects_divergence(tmp_path, monkeypatch):
    """文档里的片段被改歪时，检查脚本要**报错**而不是放行。

    这是这个脚本的全部价值：一个只会说「一致」的检查等于没有检查。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_proto_contract", ROOT / "scripts" / "check_proto_contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 拿一份改歪的文档副本喂给它
    doc = tmp_path / "design.md"
    original = (ROOT / "app" / "design" / "v4-tools-skills.md").read_text(
        encoding="utf-8"
    )
    doc.write_text(original.replace("string skill = 1;", "string skill = 999;"), encoding="utf-8")

    block = module.extract_block(doc)
    assert block is not None
    assert block != PROTO_FILE.read_text(encoding="utf-8")
    # diff 必须指出差异——空 diff 说明脚本坏了
    assert module.diff(PROTO_FILE.read_text(encoding="utf-8"), block, "a", "b")


def test_generated_stubs_are_importable():
    """生成物能 import，且消息与服务名对得上。"""
    from webagent.studio.v1 import studio_pb2, studio_pb2_grpc

    request = studio_pb2.ExecRequest(
        skill="demo",
        script="scripts/extract.py",
        language="python",
        content="print(1)",
        argv=["--x"],
        timeout_ms=1000,
    )
    assert request.skill == "demo"
    assert list(request.argv) == ["--x"]
    # 枚举值与契约一致（COMPLETED=1 / TIMEOUT=3）
    assert studio_pb2.EXEC_STATUS_UNSPECIFIED == 0
    assert studio_pb2.EXEC_STATUS_COMPLETED == 1
    assert studio_pb2.EXEC_STATUS_FAILED == 2
    assert studio_pb2.EXEC_STATUS_TIMEOUT == 3
    assert studio_pb2.EXEC_STATUS_LIMITED == 4
    assert studio_pb2.EXEC_STATUS_CANCELLED == 5
    # 服务桩存在
    assert hasattr(studio_pb2_grpc, "StudioStub")
    assert hasattr(studio_pb2_grpc, "StudioServicer")


def test_generated_package_is_importable_from_repo_root_only():
    """桩代码包在**仓库根**，不在 app/ 下面。

    studio 服务与 web_agent 共用这一份——studio 容器里不装 web_agent 的代码，
    只把这个目录拷进去就能用（§22.8.3：契约文件在仓库里只有一份）。
    """
    assert (ROOT / "webagent" / "__init__.py").exists()
    assert (ROOT / "webagent" / "studio" / "v1" / "studio_pb2.py").exists()
    assert (ROOT / "webagent" / "studio" / "v1" / "studio_pb2_grpc.py").exists()
    for level in ("webagent", "webagent/studio", "webagent/studio/v1"):
        assert (ROOT / level / "__init__.py").exists(), f"{level} 缺 __init__.py"

"""管理端容器入口的行为（迁移在启动时自动跑）。

这层以前不存在，代价是「首次部署必然失败一次」：容器直接起 uvicorn，第一件事
查 ``super_admin_account`` 就抛 ``UndefinedTable``。这些用例把那次事故的几个
前提钉住：迁移打的是**管理端库**、失败不静默、多副本重复执行不算失败。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import admin_entrypoint as entry

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.admin"


class FakeResult:
    """替身：只要 returncode 与 stdout。"""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class Recorder:
    """记录每次被调用的 argv 与环境，按队列返回结果。"""

    def __init__(self, *results: FakeResult) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._results = list(results)

    def __call__(self, argv, env=None, **kwargs):  # noqa: ANN001 - 模拟 subprocess.run
        self.calls.append((list(argv), dict(env or {})))
        if self._results:
            return self._results.pop(0)
        return FakeResult(0)


def run_main(recorder: Recorder, argv=None, environ=None) -> tuple[int, list[tuple[str, list[str]]]]:
    executed: list[tuple[str, list[str]]] = []

    def fake_exec(program: str, args: list[str]) -> None:
        executed.append((program, list(args)))

    code = entry.main(
        argv=argv,
        environ=environ if environ is not None else {},
        runner=recorder,
        exec_to=fake_exec,
    )
    return code, executed


# ---------------------------------------------------------------- 目标库选择


def test_resolve_database_url_prefers_admin_url():
    """迁移要打管理端库，不是客户端库。"""
    env = {
        "DATABASE_URL": "mysql+asyncmy://app@mysql/webagent",
        "ADMIN_DATABASE_URL": "postgresql+psycopg://admin@pg/webagent_admin",
    }
    assert entry.resolve_database_url(env) == "postgresql+psycopg://admin@pg/webagent_admin"


def test_resolve_database_url_falls_back_and_allows_empty():
    """没配独立库时回落到 DATABASE_URL；两个都没有就返回空串。"""
    assert entry.resolve_database_url({"DATABASE_URL": "sqlite:///x.db"}) == "sqlite:///x.db"
    assert entry.resolve_database_url({}) == ""


def test_migrations_disabled_parsing():
    """只有明确的假值才算关闭；留空按默认（开启）处理。"""
    for raw in ("", "1", "true", "yes", "on", "TRUE"):
        assert entry.migrations_disabled({"ADMIN_MIGRATE_ON_START": raw}) is False, raw
    for raw in ("0", "false", "no", "off", " FALSE "):
        assert entry.migrations_disabled({"ADMIN_MIGRATE_ON_START": raw}) is True, raw


# ---------------------------------------------------------------- 主流程


def test_main_migrates_admin_database_then_execs_uvicorn():
    """正常路径：先迁移（环境里的 DATABASE_URL 被换成管理端库），再 exec uvicorn。"""
    recorder = Recorder(FakeResult(0))
    code, executed = run_main(
        recorder,
        environ={"ADMIN_DATABASE_URL": "postgresql+psycopg://admin@pg/db"},
    )

    assert code == 0
    assert len(recorder.calls) == 1
    argv, env = recorder.calls[0]
    assert argv[1:] == ["-m", "alembic", "upgrade", "head"]
    assert env["DATABASE_URL"] == "postgresql+psycopg://admin@pg/db"
    assert executed == [("uvicorn", list(entry.DEFAULT_COMMAND))]


def test_main_passes_through_custom_command():
    """`docker run image <cmd>` 时要把命令原样交出去，别被默认命令盖掉。"""
    recorder = Recorder(FakeResult(0))
    code, executed = run_main(
        recorder,
        argv=["python", "-m", "scripts.admin_healthcheck"],
        environ={"ADMIN_DATABASE_URL": "postgresql+psycopg://admin@pg/db"},
    )

    assert code == 0
    assert executed == [("python", ["python", "-m", "scripts.admin_healthcheck"])]


def test_main_refuses_to_start_when_migration_fails():
    """迁移真的失败时必须退出，不能带着旧结构对外服务。"""
    recorder = Recorder(
        FakeResult(1),  # upgrade 失败
        FakeResult(0, "8a5e3d0c2b11\n"),  # current：停在旧版本
        FakeResult(0, "4e8c2a1d7f90 (head)\n"),  # heads
    )
    code, executed = run_main(recorder, environ={"ADMIN_DATABASE_URL": "postgresql+psycopg://a/b"})

    assert code == 1
    assert executed == []


def test_main_tolerates_concurrent_migration_from_another_replica():
    """命令失败但库已经是 head：另一个副本刚迁移完，继续启动。"""
    recorder = Recorder(
        FakeResult(1),
        FakeResult(0, "4e8c2a1d7f90 (head)\n"),
        FakeResult(0, "4e8c2a1d7f90 (head)\n"),
    )
    code, executed = run_main(recorder, environ={"ADMIN_DATABASE_URL": "postgresql+psycopg://a/b"})

    assert code == 0
    assert executed, "已是 head 时应当继续启动"


def test_main_requires_a_database_url():
    """两个库地址都没配时，宁可报错也不要连一个不存在的默认库。"""
    recorder = Recorder()
    code, executed = run_main(recorder, environ={})

    assert code == 2
    assert recorder.calls == []
    assert executed == []


def test_main_respects_opt_out_switch():
    """`ADMIN_MIGRATE_ON_START=0` 时不迁移，直接启动（迁移由外部流水线负责）。"""
    recorder = Recorder()
    code, executed = run_main(
        recorder,
        environ={"ADMIN_DATABASE_URL": "postgresql+psycopg://a/b", "ADMIN_MIGRATE_ON_START": "0"},
    )

    assert code == 0
    assert recorder.calls == []
    assert executed, "关闭迁移后仍要启动"


def test_is_at_head_ignores_alembic_log_noise():
    """`alembic current` 的输出里混着 INFO 行，不能被当成 revision。"""
    recorder = Recorder(
        FakeResult(
            0,
            "INFO  [alembic.runtime.migration] Context impl PostgreSQLImpl.\n"
            "4e8c2a1d7f90 (head)\n",
        ),
        FakeResult(0, "4e8c2a1d7f90 (head)\n"),
    )
    assert entry.is_at_head(recorder, {"DATABASE_URL": "postgresql+psycopg://a/b"}) is True


# ---------------------------------------------------------------- 镜像契约


def test_dockerfile_runs_migrations_from_entrypoint():
    """镜像里必须真的把入口接上：ENTRYPOINT 指向脚本，脚本也被 COPY 进去。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "scripts/admin_entrypoint.py" in text
    assert 'ENTRYPOINT ["python", "-m", "scripts.admin_entrypoint"]' in text


def test_entrypoint_module_imports_without_side_effects():
    """导入入口模块不能连库、不能 exec —— 否则测试与工具链都会被它拖下水。"""
    result = subprocess.run(
        [__import__("sys").executable, "-c", "import scripts.admin_entrypoint"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

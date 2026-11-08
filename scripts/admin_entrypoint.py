"""管理端容器入口：先跑数据库迁移，再把进程交给 uvicorn。

为什么要做进容器入口：``services/admin_web/README.md`` 的启动顺序写的是
「先 ``alembic upgrade head``，再起 uvicorn」，但镜像里没人做第一步。首次部署
时 admin-web 启动即查 ``super_admin_account``，表不存在就抛
``psycopg.errors.UndefinedTable`` 然后退出——看起来像代码坏了，实际只是少跑
了一次迁移。

三个刻意的选择：

1. **迁移目标走 ``ADMIN_DATABASE_URL``**：``alembic/env.py`` 读的是
   ``settings.database_url``（即 ``DATABASE_URL``），而管理端用的是**独立**库。
   这里只在**迁移子进程**的环境里把 ``DATABASE_URL`` 覆盖成管理端地址，
   不改本进程的环境——否则应用自己会连错库。
2. **迁移失败不静默**：唯一例外是「命令失败，但当前版本已经是 head」，那说明
   另一个副本刚刚迁移完（多副本同时启动）。真的失败就退出，绝不带着旧结构
   对外服务。
3. **迁移完 ``exec`` 掉自己**：让 uvicorn 直接成为容器里的主进程，SIGTERM
   才能传到它手上；留一层父进程会拖慢优雅停机，也是僵尸进程的来源。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence

DEFAULT_COMMAND = (
    "uvicorn",
    "services.admin_web.app:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8001",
)

_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_REVISION_LINE = re.compile(r"^\s*([0-9A-Za-z_]+)")


def resolve_database_url(environ: Mapping[str, str]) -> str:
    """取迁移目标库：优先 ``ADMIN_DATABASE_URL``，没有才回落到 ``DATABASE_URL``。

    返回值可能是空串（两个都没配），由调用方决定怎么处理。
    """
    admin = (environ.get("ADMIN_DATABASE_URL") or "").strip()
    if admin:
        return admin
    return (environ.get("DATABASE_URL") or "").strip()


def migrations_disabled(environ: Mapping[str, str]) -> bool:
    """``ADMIN_MIGRATE_ON_START`` 是否明确关闭（默认开启）。

    留这个开关是为了「迁移由外部流水线负责」的部署方式：那时重复迁移既不必要，
    也可能和多副本抢锁。
    """
    raw = (environ.get("ADMIN_MIGRATE_ON_START") or "").strip().lower()
    return raw in _FALSE_VALUES


def _revisions(runner, args: list[str], env: Mapping[str, str]) -> set[str]:
    """跑一条 alembic 只读命令，把输出里的 revision 抓出来。"""
    result = runner(
        [sys.executable, "-m", "alembic", *args],
        env=dict(env),
        capture_output=True,
        text=True,
    )
    if getattr(result, "returncode", 1) != 0:
        return set()
    found: set[str] = set()
    for line in (result.stdout or "").splitlines():
        match = _REVISION_LINE.match(line)
        if match and not line.strip().lower().startswith(("info", "running", "context")):
            found.add(match.group(1))
    return found


def is_at_head(runner, env: Mapping[str, str]) -> bool:
    """当前库是否已经对齐 head。

    用来区分「迁移失败」与「另一个副本刚迁完，这一份重复执行失败」。
    """
    current = _revisions(runner, ["current"], env)
    heads = _revisions(runner, ["heads"], env)
    return bool(heads) and heads <= current


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner=subprocess.run,
    exec_to=os.execvp,
) -> int:
    """跑迁移，然后把进程换成 ``argv``。

    ``runner`` 与 ``exec_to`` 可注入，测试里不需要真的连库或真的 exec。
    """
    env = dict(os.environ if environ is None else environ)
    command = list(DEFAULT_COMMAND if argv is None else argv)

    if migrations_disabled(env):
        print("[entrypoint] ADMIN_MIGRATE_ON_START 已关闭，跳过数据库迁移", flush=True)
    else:
        target = resolve_database_url(env)
        if not target:
            print(
                "[entrypoint] 既没有 ADMIN_DATABASE_URL 也没有 DATABASE_URL，无法迁移",
                file=sys.stderr,
                flush=True,
            )
            return 2
        migration_env = {**env, "DATABASE_URL": target}
        print("[entrypoint] 迁移数据库结构（alembic upgrade head）…", flush=True)
        result = runner(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=migration_env,
        )
        if getattr(result, "returncode", 1) != 0:
            if is_at_head(runner, migration_env):
                print(
                    "[entrypoint] 迁移命令返回非零，但库已经是 head——"
                    "可能另一个副本刚刚迁移完，继续启动",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    "[entrypoint] 数据库迁移失败，管理端不会带着旧结构启动",
                    file=sys.stderr,
                    flush=True,
                )
                return getattr(result, "returncode", 1) or 1
        print("[entrypoint] 迁移完成", flush=True)

    exec_to(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""PostgreSQL base backup + point-in-time restore helper.

This script intentionally uses PostgreSQL client binaries instead of database SQL:
``pg_basebackup`` captures the physical cluster and the deployment's
``pitr-archive-wal.sh`` keeps every WAL segment needed for replay.

Examples::

    python scripts/pg_pitr.py basebackup --target /backup/base-20261105
    python scripts/pg_pitr.py restore \
      --base /backup/base-20261105 \
      --target /tmp/pitr-restore \
      --archive-dir /backup/wal \
      --target-time '2026-11-05 15:30:00+08'

Connection parameters come from the standard ``PG*`` environment variables. No
password or DSN is written to the repository or command output.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


class PitrError(Exception):
    """Backup or restore precondition failed."""


def basebackup(target: Path) -> None:
    """Create a physical base backup in an empty target directory."""
    if target.exists() and any(target.iterdir()):
        raise PitrError(f"目标目录非空：{target}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        "pg_basebackup",
        "--pgdata",
        str(target),
        "--format=plain",
        "--wal-method=stream",
        "--checkpoint=fast",
        "--progress",
    ]
    subprocess.run(command, check=True)
    marker = target / ".webagent-basebackup"
    marker.write_text("created by scripts/pg_pitr.py\n", encoding="utf-8")
    os.chmod(marker, 0o600)


def prepare_restore(
    *,
    base: Path,
    target: Path,
    archive_dir: Path,
    target_time: str,
    target_action: str = "promote",
) -> Path:
    """Copy a base backup and write PostgreSQL recovery settings.

    Returns:
        The recovery signal path.
    """
    if not base.is_dir():
        raise PitrError(f"基础备份不存在：{base}")
    if not (base / "backup_label").exists() and not (base / "backup_label.old").exists():
        raise PitrError(f"{base} 不像 pg_basebackup 产物：缺少 backup_label")
    if target.exists() and any(target.iterdir()):
        raise PitrError(f"恢复目标目录非空：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base, target, dirs_exist_ok=True)
    marker = target / ".webagent-basebackup"
    if marker.exists():
        marker.unlink()

    archive_literal = str(archive_dir).replace("'", "''")
    time_literal = target_time.replace("'", "''")
    settings = (
        "\n# managed by scripts/pg_pitr.py\n"
        f"restore_command = 'cp {archive_literal}/%f %p'\n"
        f"recovery_target_time = '{time_literal}'\n"
        f"recovery_target_action = '{target_action}'\n"
    )
    with (target / "postgresql.auto.conf").open("a", encoding="utf-8") as handle:
        handle.write(settings)
    signal = target / "recovery.signal"
    signal.write_text("", encoding="utf-8")
    return signal


def _parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("basebackup", help="create a physical base backup")
    backup.add_argument("--target", required=True, type=Path)

    restore = sub.add_parser("restore", help="prepare a PITR recovery directory")
    restore.add_argument("--base", required=True, type=Path)
    restore.add_argument("--target", required=True, type=Path)
    restore.add_argument("--archive-dir", required=True, type=Path)
    restore.add_argument("--target-time", required=True)
    restore.add_argument(
        "--target-action", choices=("promote", "shutdown", "pause"), default="promote"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one PITR utility command."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "basebackup":
            basebackup(args.target)
        else:
            prepare_restore(
                base=args.base,
                target=args.target,
                archive_dir=args.archive_dir,
                target_time=args.target_time,
                target_action=args.target_action,
            )
    except (PitrError, OSError, subprocess.CalledProcessError) as exc:
        print(f"pg_pitr: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

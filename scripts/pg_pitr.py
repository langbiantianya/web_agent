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


def _require_backup(path: Path) -> None:
    """Validate a directory produced by this helper."""
    if not path.is_dir():
        raise PitrError(f"备份目录不存在：{path}")
    if not (path / ".webagent-basebackup").is_file():
        raise PitrError(
            f"{path} 不是 scripts/pg_pitr.py 创建的备份：缺少 .webagent-basebackup"
        )


def basebackup(target: Path, *, reference: Path | None = None) -> None:
    """Create a full or incremental physical backup in an empty target."""
    if target.exists() and any(target.iterdir()):
        raise PitrError(f"目标目录非空：{target}")
    if reference is not None:
        _require_backup(reference)
        manifest = reference / "backup_manifest"
        if not manifest.is_file():
            raise PitrError(f"增量备份的参考目录缺少 backup_manifest：{reference}")
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
    if reference is not None:
        command.append(f"--incremental={reference / 'backup_manifest'}")
    subprocess.run(command, check=True)
    if not (target / "backup_manifest").is_file():
        raise PitrError(f"pg_basebackup 没有生成 backup_manifest：{target}")
    marker = target / ".webagent-basebackup"
    kind = "incremental" if reference is not None else "full"
    marker.write_text(
        f"format=1\nkind={kind}\ncreated-by=scripts/pg_pitr.py\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)


def prepare_restore(
    *,
    base: Path,
    target: Path,
    archive_dir: Path,
    target_time: str,
    target_action: str = "promote",
    incrementals: tuple[Path, ...] | list[Path] = (),
) -> Path:
    """Reconstruct a base backup and write PostgreSQL recovery settings.

    Returns:
        The recovery signal path.
    """
    _require_backup(base)
    for incremental in incrementals:
        _require_backup(incremental)
    if target.exists() and any(target.iterdir()):
        raise PitrError(f"恢复目标目录非空：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if incrementals:
        subprocess.run(
            [
                "pg_combinebackup",
                "--output",
                str(target),
                str(base),
                *(str(path) for path in incrementals),
            ],
            check=True,
        )
    else:
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
    backup.add_argument(
        "--reference",
        type=Path,
        help="previous backup directory; creates a PostgreSQL incremental backup",
    )

    restore = sub.add_parser("restore", help="prepare a PITR recovery directory")
    restore.add_argument("--base", required=True, type=Path)
    restore.add_argument("--target", required=True, type=Path)
    restore.add_argument("--archive-dir", required=True, type=Path)
    restore.add_argument("--target-time", required=True)
    restore.add_argument(
        "--incremental",
        action="append",
        default=[],
        type=Path,
        help="incremental backup in chain order; may be repeated",
    )
    restore.add_argument(
        "--target-action", choices=("promote", "shutdown", "pause"), default="promote"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one PITR utility command."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "basebackup":
            basebackup(args.target, reference=args.reference)
        else:
            prepare_restore(
                base=args.base,
                target=args.target,
                archive_dir=args.archive_dir,
                target_time=args.target_time,
                target_action=args.target_action,
                incrementals=args.incremental,
            )
    except (PitrError, OSError, subprocess.CalledProcessError) as exc:
        print(f"pg_pitr: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

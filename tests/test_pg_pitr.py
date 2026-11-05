"""PostgreSQL physical backup and PITR preparation helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.pg_pitr import PitrError, prepare_restore


def _fake_base(path: Path) -> None:
    """Build the minimum filesystem shape of a base backup."""
    path.mkdir()
    (path / "backup_label").write_text("START WAL LOCATION: 0/1\n", encoding="utf-8")
    (path / "PG_VERSION").write_text("18\n", encoding="utf-8")
    (path / ".webagent-basebackup").write_text("marker\n", encoding="utf-8")


def test_prepare_restore_writes_recovery_signal_and_settings(tmp_path):
    """PITR preparation must not modify the source base backup."""
    base = tmp_path / "base"
    target = tmp_path / "restore"
    _fake_base(base)
    signal = prepare_restore(
        base=base,
        target=target,
        archive_dir=tmp_path / "wal",
        target_time="2026-11-05 08:00:00+00",
    )
    assert signal.exists()
    assert (target / "backup_label").exists()
    assert (base / ".webagent-basebackup").exists()
    assert not (target / ".webagent-basebackup").exists()
    auto = (target / "postgresql.auto.conf").read_text(encoding="utf-8")
    assert "recovery_target_time = '2026-11-05 08:00:00+00'" in auto
    assert "restore_command = 'cp" in auto


def test_prepare_restore_refuses_nonempty_target(tmp_path):
    """A restore must never overwrite an existing PostgreSQL data directory."""
    base = tmp_path / "base"
    target = tmp_path / "restore"
    _fake_base(base)
    target.mkdir()
    (target / "important").write_text("keep", encoding="utf-8")
    with pytest.raises(PitrError):
        prepare_restore(
            base=base,
            target=target,
            archive_dir=tmp_path / "wal",
            target_time="2026-11-05 08:00:00+00",
        )
    assert (target / "important").read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "posix", reason="POSIX archive command")
def test_archive_wal_is_idempotent_and_rejects_conflicts(tmp_path):
    """PostgreSQL retries archive_command; identical retries pass, conflicts fail."""
    source = tmp_path / "000000010000000000000001"
    source.write_bytes(b"wal-one")
    archive = tmp_path / "archive"
    env = {**os.environ, "PG_ARCHIVE_DIR": str(archive)}
    command = [
        "sh",
        str(Path(__file__).resolve().parents[1] / "deploy/pitr-archive-wal.sh"),
        str(source),
        source.name,
    ]
    subprocess.run(command, env=env, check=True)
    subprocess.run(command, env=env, check=True)
    assert (archive / source.name).read_bytes() == b"wal-one"
    source.write_bytes(b"different")
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(command, env=env, check=True)

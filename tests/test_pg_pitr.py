"""PostgreSQL physical backup and PITR preparation helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.pg_pitr import PitrError, basebackup, prepare_restore


def _fake_base(path: Path) -> None:
    """Build the minimum filesystem shape of a base backup."""
    path.mkdir()
    (path / "PG_VERSION").write_text("18\n", encoding="utf-8")
    (path / "backup_manifest").write_text("{}\n", encoding="utf-8")
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


def test_prepare_restore_requires_webagent_backup_marker(tmp_path):
    """An arbitrary PostgreSQL data directory must not be treated as a backup."""
    base = tmp_path / "base"
    base.mkdir()
    (base / "backup_label").write_text("not enough\n", encoding="utf-8")
    with pytest.raises(PitrError, match=r"\.webagent-basebackup"):
        prepare_restore(
            base=base,
            target=tmp_path / "restore",
            archive_dir=tmp_path / "wal",
            target_time="2026-11-05 08:00:00+00",
        )


def test_basebackup_supports_an_incremental_reference(tmp_path, monkeypatch):
    """Incremental backup must pass the previous manifest to pg_basebackup."""
    reference = tmp_path / "full"
    target = tmp_path / "incremental"
    _fake_base(reference)
    commands: list[list[str]] = []

    def _run(command, *, check):
        """Record the command and mimic the generated manifest."""
        assert check is True
        commands.append(command)
        (target / "backup_manifest").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", _run)
    basebackup(target, reference=reference)
    assert commands[0][:2] == ["pg_basebackup", "--pgdata"]
    assert f"--incremental={reference / 'backup_manifest'}" in commands[0]
    marker = (target / ".webagent-basebackup").read_text(encoding="utf-8")
    assert "kind=incremental" in marker


def test_prepare_restore_combines_incremental_chain(tmp_path, monkeypatch):
    """PITR reconstruction must use pg_combinebackup in chain order."""
    full = tmp_path / "full"
    first = tmp_path / "inc-1"
    second = tmp_path / "inc-2"
    target = tmp_path / "restore"
    for path in (full, first, second):
        _fake_base(path)
    commands: list[list[str]] = []

    def _run(command, *, check):
        """Record combinebackup and create its output directory."""
        assert check is True
        commands.append(command)
        target.mkdir()
        (target / "PG_VERSION").write_text("18\n", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", _run)
    signal = prepare_restore(
        base=full,
        target=target,
        archive_dir=tmp_path / "wal",
        target_time="2026-11-05 08:00:00+00",
        incrementals=[first, second],
    )
    assert commands == [
        [
            "pg_combinebackup",
            "--output",
            str(target),
            str(full),
            str(first),
            str(second),
        ]
    ]
    assert signal.exists()
    assert (target / "recovery.signal").exists()
    assert "recovery_target_time" in (
        target / "postgresql.auto.conf"
    ).read_text(encoding="utf-8")


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

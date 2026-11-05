# PostgreSQL PITR

PostgreSQL 的 PITR 由两部分组成：

1. `pitr-archive-wal.sh` 由数据库进程调用，把完整 WAL 段写入独立归档目录；
2. `scripts/pg_pitr.py` 创建 `pg_basebackup` 物理备份，并生成恢复目录。

## PostgreSQL 配置

把脚本以只读方式挂到数据库容器，例如 `/opt/webagent/pitr-archive-wal.sh`，
再设置：

```conf
wal_level = replica
archive_mode = on
archive_timeout = 60
archive_command = 'PG_ARCHIVE_DIR=/var/lib/postgresql/archive /opt/webagent/pitr-archive-wal.sh "%p" "%f"'
```

归档目录必须位于持久卷，并且与数据库数据目录分开备份。定期检查：

```sql
SELECT archived_count, failed_count, last_archived_wal, last_failed_wal
FROM pg_stat_archiver;
```

## 基础备份

在装有 PostgreSQL 客户端工具的主机执行：

```bash
PGHOST=127.0.0.1 PGPORT=5432 PGUSER=webagent PGDATABASE=webagent \
PGPASSWORD=... python scripts/pg_pitr.py basebackup \
  --target /backup/postgres/base-20261105T160000Z
```

密码只从进程环境传入，脚本不打印连接串。

## 时间点恢复

先在**隔离端口和目录**恢复，验证后再切换生产：

```bash
python scripts/pg_pitr.py restore \
  --base /backup/postgres/base-20261105T160000Z \
  --target /restore/postgres-1530 \
  --archive-dir /backup/postgres/wal \
  --target-time '2026-11-05 15:30:00+08'
```

目标目录会得到 PostgreSQL 的 `recovery.signal`、`restore_command` 和
`recovery_target_time`。使用一个新的 PostgreSQL 实例启动该目录，并确认恢复到
目标时刻后再切换连接。

恢复目标目录必须为空；脚本拒绝覆盖已有数据目录。归档命令在目标文件已存在时
只接受内容完全相同的情况，避免 PostgreSQL 重试时静默覆盖不同 WAL。

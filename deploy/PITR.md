# PostgreSQL PITR

PostgreSQL 的 PITR 由以下几部分组成：

1. `pitr-archive-wal.sh` 由数据库进程调用，把完整 WAL 段写入独立归档目录；
2. `scripts/pg_pitr.py` 创建全量或增量物理备份；
3. `pg_combinebackup` 把全量与增量链合成为可恢复目录；
4. 新 PostgreSQL 实例按 WAL 归档重放到目标时间。

增量备份需要 PostgreSQL 17+，生产实例需额外开启 `summarize_wal`。只支持
PostgreSQL 17 及以上的增量备份格式；全量备份与原有恢复方式保持不变。

## PostgreSQL 配置

把脚本以只读方式挂到数据库容器，例如 `/opt/webagent/pitr-archive-wal.sh`，
再设置：

```conf
wal_level = replica
archive_mode = on
archive_timeout = 60
summarize_wal = on
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

## 增量备份

增量备份必须引用上一份由本脚本创建的备份。每一代都保留完整目录和
`backup_manifest`；恢复时从全量开始按创建顺序列出全部增量：

```bash
python scripts/pg_pitr.py basebackup \
  --target /backup/postgres/inc-20261105T170000Z \
  --reference /backup/postgres/base-20261105T160000Z
```

生产建议按“周期性全量 + 更频繁增量 + 独立 WAL 归档”的组合保留；增量链过长会
增加恢复时间和单点损坏风险，需定期做全量备份并演练恢复。

## 时间点恢复

先在**隔离端口和目录**恢复，验证后再切换生产：

```bash
python scripts/pg_pitr.py restore \
  --base /backup/postgres/base-20261105T160000Z \
  --target /restore/postgres-1530 \
  --archive-dir /backup/postgres/wal \
  --incremental /backup/postgres/inc-20261105T170000Z \
  --target-time '2026-11-05 15:30:00+08'
```

`--incremental` 可重复；顺序必须从最接近全量的增量开始。目标目录会得到
PostgreSQL 的 `recovery.signal`、`restore_command` 和 `recovery_target_time`。
使用一个新的 PostgreSQL 实例启动该目录，并确认恢复到目标时刻后再切换连接。

恢复目标目录必须为空；脚本拒绝覆盖已有数据目录。归档命令在目标文件已存在时
只接受内容完全相同的情况，避免 PostgreSQL 重试时静默覆盖不同 WAL。

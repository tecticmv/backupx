# PITR verification: what is proven, what is guaranteed, how to roll out

Owner requirement: "I need 100 % sure it works and nothing in prod is getting
damaged." This page lists the evidence (version matrix, tests), the guarantees
enforced in code, what is *not* guaranteed, and the staged production rollout.

## Version matrix (end to end, throwaway containers)

Rig: `C:/Users/ali/claude-work/2026-11-01-backupx-p3/e2e` (`matrix-up.sh`,
`e2e_matrix.py`, label `backupx-p3-rig=1`). Sources run in docker with binlog
ROW, 64 KB binlog files (so the streamer goes through many rotations) and
`general_log` into `mysql.general_log`; the job server is a systemd host with
docker (streamer unit, automatic client image), storages are two MinIO
servers. For each source:

1. **auto image**: the server version (`SELECT VERSION()`) resolves the client
   image; it is pulled on the job server and pinned by digest.
2. **prereq**: the prerequisite check passes with the pinned client.
3. **rotations**: the streamer streams through >= 5 rotations; every shipped
   segment lands on both storages.
4. **anchor**: the anchored dump records its position and lands on both storages.
5. **restore**: scratch restore from the **second** storage to an exact second
   T: the row written 20 s before T is there, the row written 10 s after T is
   not, the row of the last rotation is there, and `CHECKSUM TABLE` of every
   table equals the source's at T.
6. **not a replica**: `SHOW SLAVE HOSTS` on the source is empty (mysqlbinlog
   never registers as a replica) and there is exactly one binlog-dump
   connection.
7. **read-only**: every statement backupx's login sent (general_log) is a read
   or a documented session/dump statement (below).

Run 2026-11-01 (`logs/matrix-full.log`, `logs/matrix-rerun.log`; the rerun covers the MySQL
entries and the upgrades after harness fixes - `SHOW REPLICAS` on MySQL 8.4, waiting for the
streamer to reconnect after the upgrade):

| Source | Server | Auto client image (pinned by digest) | Prereq | Rotations shipped (2 storages) | Anchor | Restore to the second (storage 2) | CHECKSUM TABLE = source at T | Not a replica | general_log of backupx |
|---|---|---|---|---|---|---|---|---|---|
| MySQL 8.0 | 8.0.46 | `percona/percona-server:8.0` | pass | 6 files | pass | pass | equal | pass | 121 statements, all allowed |
| MySQL 8.4, GTID off | 8.4.11 | `percona/percona-server:8.4` | pass | 6 files | pass | pass | equal | pass | 101, all allowed |
| MySQL 8.4, GTID on | 8.4.11 | `percona/percona-server:8.4` | pass | 6 files | pass | pass | equal | pass | 102, all allowed |
| MariaDB 10.3 | 10.3.39 | `mariadb:10.3` | pass | 6 files | pass | pass | equal | pass | 75, all allowed |
| MariaDB 10.6 | 10.6.28 | `mariadb:10.6` | pass | 6 files | pass | pass | equal | pass | 75, all allowed |
| MariaDB 10.11 | 10.11.19 | `mariadb:10.11` | pass | 6 files | pass | pass | equal | pass | 75, all allowed |
| MariaDB 11.4 | 11.4.13 | `mariadb:11.4` | pass | 6 files | pass | pass | equal | pass | 75, all allowed |

Each restore also checked: the row written 20 s before T present, the row written 10 s after T
absent, the last rotation's rows present. Earlier P2/P3 rigs add MySQL 8.4 + GTID with the
percona client on a systemd host, MariaDB 10.3/10.11 host and docker installs and PostgreSQL 10
(WAL-G) restores to the exact second.

Restore drill (`e2e_drill.py`, MySQL 8.4 job): clean drill passes (2 of 2 tables identical, job
card shows *verified*); a production change made with `sql_log_bin=0` -> the drill fails naming
`app.t2`, critical alert, health issue `restore_drill_failed`; the drill's statements on
production were all reads (`CHECKSUM TABLE`, SELECT); scratch copies removed.

Mid-chain upgrades (same data directory, new server image):

| Upgrade | Detected | Client re-pinned | State until new anchor | New chain after anchored dump | Pre-upgrade time restorable (checksums) | general_log after upgrade |
|---|---|---|---|---|---|---|
| MySQL 8.0.46 -> 8.4.11 | `server version changed (mysql 8.0 -> mysql 8.4)` | `percona/percona-server@sha256:95d2...` (8.4) | broken | ok | yes, equal | 87, all allowed |
| MariaDB 10.6.28 -> 10.11.19 | `server version changed (mariadb 10.6 -> mariadb 10.11)` | `mariadb@sha256:d769...` (10.11) | broken | ok | yes, equal | 67, all allowed |

Detection happens on the first tick after the streamer has reconnected to the upgraded server
and recorded its version (the streamer restarts on its own; with a changed address the tick
rewrites its option file and restarts it). Binlogs written by the new version before that tick
belong to the old chain; restores into that range use the old dump and replay the newer
server's binlogs (worked in the rig, not a supported guarantee). The first run of the MariaDB
upgrade showed this: the break was recorded one tick later, after the post-upgrade dump, and
everything still restored correctly.

## Guarantees (enforced in code, tested)

**(a) Backup, streaming, prerequisite, health, verify and drill paths only read
production.** `tests/test_prod_safety.py` renders every script these paths send
and fails on any statement other than SELECT / SHOW / SET SESSION and on any
DDL/DML/GRANT/SET GLOBAL/FLUSH/RESET/PURGE/CHANGE MASTER/START REPLICA keyword.
The matrix additionally records what actually reached each server
(general_log). What a dump legitimately sends, all session-scoped or
read-consistency statements:

| Statement | Why | When |
|---|---|---|
| `SET SESSION ...`, `SET NAMES`, `SET optimizer_switch` (no GLOBAL) | client/dump session settings | every connection |
| `START TRANSACTION WITH CONSISTENT SNAPSHOT`, `SAVEPOINT`, `ROLLBACK TO SAVEPOINT`, `RELEASE SAVEPOINT` | `mysqldump --single-transaction`: one consistent read | nightly dump |
| `FLUSH TABLES`, `FLUSH TABLES WITH READ LOCK`, `UNLOCK TABLES` | `--source-data=2` / `--master-data=2`: a global read lock held only while the binlog position is read (milliseconds when no long query runs; a long-running query delays it) | anchored nightly dump only |
| `CHECKSUM TABLE` | restore drill (reads every row) | weekly drill |

backupx never runs `FLUSH BINARY LOGS`, `PURGE BINARY LOGS` or `RESET MASTER`
(the e2e harness flushes binlogs as root to force rotations; that is not
backupx). The dump option is `=2`, so the position is a comment and nothing is
executed on load. `--lock-all-tables`, `--flush-logs`, `--delete-master-logs`
are never used (tested).

**(b) The streamer only reads binlogs.** `mysqlbinlog
--read-from-remote-server --raw --stop-never` with a per-job
`--connection-server-id` (3e9 + crc32(job id): unique per job and far above
real server ids). It sends a binlog dump request; it does not register as a
replica, configures nothing on the source, holds no locks and never purges
(matrix: `SHOW SLAVE HOSTS` empty). Binlog retention on the source is the
source's own setting.

**(c) Restores never target production** except the gated replace / undo flow
of the restore wizard (typed database name + step-up + plan hash, pre-restore
backup, automatic rollback copy). `tests/test_prod_safety.py`: production
imports (`bximport`) only exist in the replace and rollback scripts;
production is otherwise only queried by the read-only verify. Drills restore
into scratch containers on the backupx server.

**(d) Configuration changes and restarts** only through the maintenance flow:
typed confirmation + step-up + plan hash, re-derived inside the task (changed
-> nothing done), fresh backup, config backup, validation before any restart,
health check, automatic rollback (never while the server is recovering)
(`tests/test_pitr_enable.py`, e2e: validation blocks, crash -> rollback,
unhealthy -> ALTER SYSTEM RESET). One-time admin logins are never stored.

**Client containers** (automatic matching) run `--read-only --tmpfs /tmp
--cap-drop ALL --security-opt no-new-privileges`, no published ports;
credentials reach them on stdin into the tmpfs, never in argv or env.

## Not guaranteed / limits

- A restore is only as good as the binlogs: changes made with `sql_log_bin=0`
  (or filtered out of the binlog) are not in point-in-time restores. The
  drill detects this for tables that were quiet during the drill (tested).
- Binlogs the server purged before the streamer read them (DigitalOcean keeps
  ~10-15 min) are a gap: the chain breaks until the next anchored dump (alerted).
- The anchored dump's `FLUSH TABLES WITH READ LOCK` waits for running queries;
  on a server with very long queries it can stall writes briefly. If FTWRL is
  not allowed, the dump falls back to a plain one and PITR goes unanchored
  (alerted), never failing the backup.
- `CHECKSUM TABLE` values are compared between the same server version (the
  scratch image matches production); tables written to during a drill are
  reported, not judged; tables above `RESTORE_DRILL_MAX_TABLE_ROWS` are
  compared by row count only.
- PostgreSQL (WAL-G) is covered by the P2/P3 rigs (PostgreSQL 10, exact-time
  restore, maintenance flow); PostgreSQL 12+ restore layout (`recovery.signal`)
  is not tested end to end.
- Automatic matching needs docker on the job's server; running the client
  tools on the backupx host instead is not built (such jobs keep host
  binaries or a manual image). DigitalOcean only accepts trusted sources: the
  prerequisite check explains adding the server's public IP (manual).

## Staged production rollout

1. **Deploy with automatic matching OFF** for the existing jobs (the migration
   sets `pitr_client_auto = false`; E-Petition keeps the host mysql 8.4.13
   client, Koha the host MariaDB 10.6 client, DSpace WAL-G). Nothing about
   their streaming, dumps or configuration changes.
2. **Weekly restore drill** (`RESTORE_DRILL_ENABLED=true`, Saturday 03:30
   scheduler time, one job at a time, scratch copy on the backupx server,
   critical alert on failure, *Verified restore* on the job card). Run one by
   hand from each PITR job's page right after the deploy.
3. **Switch one job at a time to automatic matching** after it has a passing
   drill: PITR setup wizard -> only the *Match the client tools* item ->
   apply; then watch the next ticks, the next nightly anchored dump and the
   next drill before the next job.
4. Database configuration changes through the wizard only in an agreed window
   (use the scheduled-window option).

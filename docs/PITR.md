# Point-in-time recovery (PITR)

Restore a database job to any second between its oldest kept full backup and
a few minutes ago, instead of only to last night's dump.

| Engine | How | Where it runs |
|---|---|---|
| MySQL / MariaDB | continuous binlog streaming (`mysqlbinlog --read-from-remote-server --raw --stop-never`), shipped with restic | systemd unit on the job's SSH server, installed by backupx |
| PostgreSQL | WAL-G (`archive_command` → `wal-g wal-push`, nightly `wal-g backup-push`) | on the database host |

Policy (changed in P3): backupx may change database configuration and restart
a database, but **only** through the confirmed maintenance flow of the PITR
setup wizard (*Enable PITR* on the job card, `/jobs/<id>/pitr-setup`): exact
plan + downtime estimate, typed database name, password re-entry (step-up),
plan hash; a fresh backup first (unless one is under 1 h old), a config
backup, validation, the restart (now or in a scheduled window), a health check
and automatic rollback on failure (never while the server is still in crash
recovery). Docker databases only when a conf directory is bind-mounted;
managed databases (DigitalOcean, RDS, ...) never. Nothing else touches database
configuration: enabling PITR in the job form still only installs the streamer
(MySQL) or wal-g (PostgreSQL). Details: `docs/UX-OVERHAUL.md` (P3), proof:
`docs/PITR-VERIFICATION.md`.

## MySQL / MariaDB

### How it works

1. **Streamer** (`backupx-pitr-binlog@<job>.service`, script
   `/usr/local/lib/backupx-pitr/binlog-streamer.sh`) keeps a replication
   connection open and writes byte-exact binlog copies to
   `/var/lib/backupx-pitr/<job>/spool/`. It works independent of the
   provider (DigitalOcean's own PITR is not used), so it also covers servers
   that aren't on DO.
   - Resumes after a restart from the newest spool file (re-fetched whole), or
     the file after the last shipped one. If the server already purged what it
     needs (e.g. DigitalOcean keeps only ~10–15 min of binlogs), it records a
     *gap* and continues with the oldest file it can get.
   - Uses a unique replica id per job (`--connection-server-id`, 3e9 + crc32).
   - A watchdog compares the server's current binlog with the spool every
     minute; out of sync for 5 minutes → restart. In sync → touches
     `heartbeat`, which lets backupx count the open file as complete up to now.
   - Credentials: `/etc/backupx-pitr/<job>/my.cnf` (root, 0600). When the
     client runs in docker the file goes into the container on stdin.
     `mysqlbinlog` gets it as `--defaults-file` (only that file): MariaDB
     10.3's `/etc/mysql/mariadb.conf.d/50-client.cnf` puts
     `default-character-set` in `[client]`, which mysqlbinlog rejects
     (`--no-defaults` can't be combined with `--defaults-extra-file`).
2. **Shipping** (every *interval* minutes, own scheduler pool `pitr`, advisory
   lock `<job>:pitr`): backupx ships the closed files (all but the newest) with
   restic to the sibling repository `<prefix>-binlog` on **each** of the job's
   storages, plus a copy of the open file ("partial", only when it changed).
   Spool files are deleted only after every storage has them. Tags:
   `pitr binlog first:<file> last:<file> until:<time> chain:<n>`.
3. **Anchor**: with PITR on, the nightly mysqldump adds `--source-data=2`
   (`--master-data=2` on MariaDB / MySQL < 8.0.26). The dump header records the
   binlog file/position of its snapshot; backupx stores it (`pitr_anchors`) and
   tags the dump snapshot `pitr-anchor binlog:<file>:<pos>`. If that dump fails
   (e.g. FTWRL not allowed, or a client that sends `SHOW MASTER STATUS` to
   MySQL 8.4) the job dumps again without it - the nightly backup never fails
   because of PITR - and PITR goes *unanchored*.
4. **Chains**: shipped files must continue the numbering on the same server.
   A hole, a streamer-reported gap or a `server_uuid` change (failover) starts a
   new chain. Times after a break become restorable once a dump lands in the
   new chain (next nightly backup, or *Run now*); earlier times stay
   restorable from the old chain.

### States (Jobs list, Dashboard) and alerts

| State | Meaning |
|---|---|
| ok | anchored, newest restorable time within `max(3 × interval, 15 min)` (`PITR_LAG_MINUTES`) |
| lagging | anchored but the newest restorable time is too old, or the streamer is down/stalled |
| broken | the chain broke and no dump has landed in the new one yet |
| unanchored | streaming fine, no dump with a position yet (normal until the first backup after enabling) |

Alerts go to the normal notification channels only on state **changes**: one
"failed" when a job enters lagging/broken (or unanchored after it was ok, or
for more than 26 h after enabling), one "recovered" when it's ok again.

### Check prerequisites

- *Root or passwordless sudo* passes only when `id -u` is 0 or
  `sudo -n id -u` prints 0 on the server; otherwise it fails with sudo's error.
- GRANT fixes name the account as `'user'@'host'` from `CURRENT_USER()`.
- A binlog client that dies before printing its version is reported as
  *mysqlbinlog starts* with its error (not as a version mismatch); one that
  only fails with the host's option files is a warning (*mysqlbinlog reads the
  host option files*), since the streamer doesn't read them.

### Automatic client matching

Per job, *Match the client tools to the server automatically* (wizard item, or
`pitr_client_auto`): MySQL X.Y -> `percona/percona-server:X.Y`, MariaDB X.Y ->
`mariadb:X.Y` (nearest newer supported line + a warning otherwise), pulled on
the job's server and pinned by digest (`pitr_client_image_digest`); re-pinned
only when the server's major.minor changes. Off for existing jobs: they keep
their host binaries / manual client image until switched. The containers run
read-only, without capabilities or published ports; credentials go in on stdin
to a tmpfs.

### Server upgrades

A change of the server's major.minor (e.g. 8.0 -> 8.4, 10.6 -> 10.11) starts a
new chain: times before the upgrade stay restorable from the old chain, times
after it need the next anchored dump (state *broken* until then; the dashboard
offers the anchored-dump fix). With automatic matching the client is re-pinned
for the new version at the same time.

### Client versions (important)

- The official `mysql:*` images ship **no mysqlbinlog**; Percona Server's
  images do (same code base and binlog format): `percona/percona-server:8.4`.
- MySQL 8.2+ removed `SHOW MASTER STATUS`. An 8.0 (Ubuntu) or MariaDB
  `mysqldump` can't record the position against 8.4: set the job's **client
  image** to `percona/percona-server:8.4` (needs docker on the streamer host),
  or install the 8.4 client from repo.mysql.com. *Check prerequisites* reports
  both.
- Docker-container databases always use their own container's image
  (`docker run --network container:<db>`), so versions match.

### Retention

`pitr_retention_days` (default 7): binlog snapshots older than the oldest
usable dump inside the period are forgotten, then the binlog repository is
pruned, during the weekly retention prune (`RETENTION_ENABLED`,
`RETENTION_DRY_RUN` apply). With no usable dump inside the period nothing is
forgotten. Dumps forgotten by the job's own keep rules stop counting as
anchors. Superseded open-file copies are forgotten as they're replaced.
Database jobs' retention now groups by host only (each anchored dump carries
its own position tag).

### Restore (runbook)

Nothing is restored in place. In the Jobs list, click the history icon of the
job → *Point-in-time restore* → pick the time (your local time; the UTC value
is shown) and the storage → *Download restore script*.

Run it on any machine with bash, docker and restic:

```bash
RESTIC_PASSWORD=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... bash restore-<job>-<time>.sh
```

(job's restic password and the storage keys; missing ones are prompted for,
nothing secret is in the script). It:

1. restores the newest anchored dump before the target time and the binlog
   segments from its position on (plus the open-file copy if needed), checks
   the sequence has no hole;
2. decodes them with `mysqlbinlog --skip-gtids --start-position=<pos>
   --stop-datetime=<target, UTC>` in the client image (TZ=UTC);
3. starts a scratch server of the production flavor/version (`mysql:8.4`,
   `mariadb:11.4`, ...) with no published ports, loads the dump, replays.

Then inspect with `docker exec -it backupx-pitr-restore-<job> mysql -uroot`,
dump what you need, and remove the container and work directory.

Without backupx (its database lost): the dump header holds the position
(`-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE=..., SOURCE_LOG_POS=...`),
and `restic -r <repo>-binlog snapshots --tag binlog` lists the segments by
`first:`/`last:` tags.

### Per-target setup

| Target | Server change | Notes |
|---|---|---|
| E-Petition (DO Managed MySQL 8.4, GTID, ROW) | none on the DB | job server must be `epetition-do` (a DO trusted source); set client image `percona/percona-server:8.4` (docker on epetition-do) or install mysql-community-client 8.4; DO keeps ~10–15 min of binlogs, so a streamer outage longer than that breaks the chain until the next dump. Verify the first anchored dump with *Run now* (FTWRL must be allowed for doadmin). |
| Koha (MariaDB in docker) | `log_bin=mysql-bin`, `server_id=1`, `binlog_format=ROW`, `expire_logs_days=2` in my.cnf + restart | the db config needs a user with `REPLICATION SLAVE`, `REPLICATION CLIENT`/`BINLOG MONITOR`, `RELOAD` |
| snipe-it / intranet / website / old-website | check with *Check prerequisites* | same as above if log_bin is off |

Spool: up to a few files per job (E-Petition ~10 MB/day total).

## PostgreSQL (WAL-G)

- **Build**: WAL-G **v2.0.1** (`wal-g-pg-ubuntu-18.04-amd64`, sha256
  pinned) on glibc < 2.31 or PostgreSQL < 12 - the DSpace host (Ubuntu 18.04,
  PostgreSQL 10.23) gets this one. v3.x builds need glibc 2.31+ and v3's CI no
  longer covers PostgreSQL 10; newer hosts get v3.0.9.
- **Installed by backupx** when PITR is enabled: `/usr/local/bin/wal-g`,
  `/etc/wal-g/wal-g.env` (postgres:postgres 0600; S3 keys of the job's
  primary storage, prefix `<prefix>-wal`), `/usr/local/bin/wal-g-archive.sh`.
- **Configuration**: the PITR setup applies these with `ALTER SYSTEM`
  (`archive_command`/`archive_timeout` with `pg_reload_conf()`, `wal_level`/
  `archive_mode` with a confirmed restart, now or in a window, rolled back with
  `ALTER SYSTEM RESET` if PostgreSQL isn't healthy afterwards). Or by hand:

  ```
  wal_level = replica
  archive_mode = on              # restart
  archive_command = '/usr/local/bin/wal-g-archive.sh %p'
  archive_timeout = 900
  ```

- **Nightly** `wal-g backup-push "$PGDATA"` on the job's schedule
  (`<job>__walg_base`, skipped until archiving is on).
- **Monitoring** every interval: `pg_stat_archiver`; `wal-g wal-verify
  integrity timeline` at most hourly. Window = oldest base backup → last
  archived WAL time; integrity FAILURE → broken.
- **Idle servers are not lagging**: PostgreSQL only switches WAL (and
  `archive_timeout` only fires) when something was written, so on a quiet
  database the last archive time can be hours old with nothing missing. Each
  tick compares `pg_walfile_name(pg_current_wal_lsn())` with
  `last_archived_wal`: equal = everything written is archived, the window is
  current up to that tick (lag 0). Otherwise lag is measured from the last
  archive time as before (25 min default). `last_failed_time` newer than
  `last_archived_time` = `archive_command` failing → lagging, with the failed
  WAL in the alert.
- **Last ship** (Jobs list): set when the archiver is healthy and the R2
  mirror (below) succeeded on that tick, even if there were no new files.
- **Second storage** (e.g. R2): `rclone copy` (never sync) of `<prefix>-wal`
  from the primary every 15 minutes, run in the backupx container. Because it
  never deletes, add a bucket lifecycle rule on that prefix (e.g. expire after
  `pitr_retention_days + 7` days).
- **Retention**: weekly `wal-g delete before FIND_FULL <now - days> --confirm`
  on the primary.
- **RustFS key**: use a key scoped to the bucket/prefix if possible (the env
  file sits on the database host).
- **Restore script**: `wal-g backup-fetch` of the newest base backup before
  the target, `recovery.conf` with `restore_command` (wal-g in the container,
  credentials via `--env-file`), `recovery_target_time`, promote, in a scratch
  `postgres:10` container.
- TODO: end-to-end test on the DSpace host after the restart window;
  PostgreSQL 12+ restore layout (`recovery.signal`).

## Restore drills

Weekly (`RESTORE_DRILL_CRON`, Saturday 03:30 scheduler time, one job at a time)
every PITR job is restored into a scratch copy on the backupx server to a
recent second and compared with production: checksums of every table read
before and after the restore (read-only), and every table that didn't change
in between must be identical in the copy. Result on the job card (*Verified
restore*) and the job page; a failure alerts and shows as a health issue. Run
one any time from the job page (*Run drill now*).

## Settings

| Variable | Default | |
|---|---|---|
| `PITR_MAX_CONCURRENT` | 2 | threads of the `pitr` scheduler pool (each borrows pooled DB connections per query) |
| `PITR_LAG_MINUTES` | 0 (auto) | lag before *lagging*; auto = max(3 × interval, 15) (PostgreSQL: max(15 + 2 × interval, 25)) |
| `PITR_SHIP_TIMEOUT` | 900 | seconds per shipment |
| `PITR_HEARTBEAT_MAX_AGE` | 300 | streamer heartbeat older than this = stalled |
| `PITR_MAINT_HEALTH_TIMEOUT` / `PITR_MAINT_RECOVERY_MAX_WAIT` | 300 / 7200 | seconds a restarted database may take to be healthy / to finish crash recovery (never rolled back meanwhile) |
| `PITR_FRESH_BACKUP_MAX_AGE` | 3600 | a successful backup newer than this counts as the backup before a restart |
| `PITR_BINLOG_BASENAME` / `PITR_BINLOG_EXPIRE_DAYS` | mysql-bin / 2 | what the wizard writes for log_bin / binlog expiry |
| `PITR_MANAGED_HOSTS` | (empty) | extra regex of managed database hosts (built in: DigitalOcean, RDS, Azure, Aiven, PlanetScale) |
| `CREDENTIAL_TTL` | 900 | seconds a one-time admin login waits for its task |
| `RESTORE_DRILL_ENABLED` / `RESTORE_DRILL_CRON` | true / `30 3 * * 6` | weekly restore drills |
| `RESTORE_DRILL_MAX_TABLE_ROWS` / `RESTORE_DRILL_SHIP_WAIT` | 5000000 / 20 | larger tables compared by count / minutes to wait for shipping |

Per job: *Point-in-time recovery* toggle, *Ship every (minutes)* (1–60),
*Keep (days)* (1–365), *Client image*.

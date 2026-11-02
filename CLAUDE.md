# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BackupX is an enterprise backup management system with a Flask backend and React frontend. It manages filesystem and database backups across multiple servers using Restic and S3-compatible storage. Fully agentless — all remote operations happen over SSH with automatic Restic provisioning.

## Commands

BackupX runs as a Docker Compose stack. From the repo root:

```bash
# Start / stop
docker compose up -d           # Start (uses bridge network)
docker compose up -d --build   # Rebuild after code changes
docker compose down            # Stop and remove containers
docker compose logs -f         # Tail logs

# Linux/production with host networking (for LAN access)
docker compose -f docker-compose.yml -f docker-compose.host.yml up -d
```

Frontend development (from `frontend/`):
```bash
npm run dev      # Vite dev server
npm run build    # Production build
npm run lint     # ESLint
```

Backend tests (requires local Python env):
```bash
pytest                                  # Run all tests
pytest tests/test_security.py           # Run specific file
pytest tests/test_security.py::TestAuthentication::test_login_success
```

## Architecture

### SSH-Only Design
- Single Flask + React app, packaged as one Docker image
- Sidecar PostgreSQL container for persistent config/history/audit storage
- All remote operations use SSH — no agents to install or maintain on target servers
- Restic is auto-provisioned on remote servers when they are added (downloads static binary via SSH)

### Project Structure
- `app/main.py` - Monolithic Flask app containing all REST API routes (`/api/*`)
- `app/db/` - Database abstraction layer (PostgreSQL)
  - `base.py` - Abstract `DatabaseBackend` interface
  - `postgres.py` - PostgreSQL implementation with connection pooling
  - `factory.py` - `create_database_backend()` factory
  - `migrate.py` - Schema migrations
- `app/audit/` - Audit logging
- `app/scheduler/locks.py` - Postgres advisory locks: scheduler leader election and per-job run lock
- `app/pitr.py`, `app/pitr_scripts.py`, `app/pitr_service.py` - Point-in-time recovery (see `docs/PITR.md`)
- `app/scheduler/distributed.py` - Older leader-election scheduler, not wired up (its tests are stale)
- `app/session.py` - Session management (filesystem or Redis)
- `frontend/` - React + TypeScript + Vite + Radix UI + Tailwind
- `tests/` - Pytest test suite
- `Dockerfile` - Multi-stage build (Node for frontend, Python for backend)
- `docker-compose.yml` - Main stack (backup-ui + postgres)
- `docker-compose.host.yml` - Override for Linux host networking

### Key Patterns
- **Database**: Factory pattern — all DB operations go through `DatabaseBackend` interface. `init_database()` (every gunicorn worker, at import) runs `init_schema` + `migrate_schema` under a Postgres advisory lock (`schema_lock()`, dedicated connection), so one worker migrates and the rest wait and no-op; unlocked, concurrent `CREATE TABLE IF NOT EXISTS` collides on the table's row type and kills gunicorn
- **SSH auth**: Three modes supported — key file path, pasted/uploaded key content, password (via `sshpass -e`, password in `$SSHPASS`). Sensitive values encrypted with Fernet
- **No secrets in argv**: argv is world-readable (`ps`, `/proc/PID/cmdline`). Remote scripts go over stdin via `_ssh_run` / `_ssh_popen` (ssh runs only the fixed `REMOTE_SHELL`, `bash -s`); never `ssh_cmd + [script]`. Inside scripts, secrets are `export`ed env vars (restic, AWS_*, PGPASSWORD) or a 0600 MySQL option file (`_mysql_option_file` + `--defaults-extra-file`), never `-p<password>` or `docker exec -e VAR=value`. Local restic/rclone calls pass secrets via `env=`
- **Notifications**: Telegram text is HTML-escaped (only our `<b>` is markup), cut to 4096 without splitting entities, and resent as plain text on HTTP 400; Slack text escapes `& < >`; code blocks can't be closed by message text
- **Sessions**: Strategy pattern — filesystem (default) or Redis for horizontal scaling
- **Scheduler**: Every gunicorn worker imports the app, but only the one holding a Postgres advisory lock starts APScheduler; it re-reads jobs/timezone from the DB every `SCHEDULER_LOCK_INTERVAL` (15s), and another worker takes over if it dies. Each backup run holds a per-job advisory lock, so a job never runs twice at once (a second trigger is skipped and logged, no notification)
- **Backup runs and the DB pool**: At most `BACKUP_MAX_CONCURRENT` scheduled runs at once; the rest queue (misfire grace `BACKUP_MISFIRE_GRACE`, so queued runs aren't dropped). Advisory locks use dedicated non-pooled connections; a run borrows a pooled connection per query (`short_lived_connections()` mode) instead of holding one for hours. The pool waits up to `DATABASE_POOL_TIMEOUT` for a free connection instead of raising at once. `run_backup` never raises: any error marks the job failed, writes history and notifies; an APScheduler error/missed listener is the last resort
- **Overdue jobs**: `/api/jobs` and `/api/dashboard/stats` flag scheduled jobs that haven't started a run since the last expected fire time (`BACKUP_OVERDUE_GRACE` ago), computed with APScheduler's CronTrigger in the scheduler timezone
- **Cron weekdays**: job schedules and `RETENTION_PRUNE_CRON` are standard crontab (day of week 0/7 = Sunday). APScheduler numbers weekdays from Monday and `CronTrigger.from_crontab` doesn't translate (3.10), so always build triggers through `app/cron.py` (`cron_fields` / `cron_trigger`), never pass the day-of-week field straight to APScheduler
- **Restic init**: The backup script runs `restic init` only when `restic cat config` reports the repo doesn't exist (exit 10, or the old-restic message); any other open error fails that storage
- **Restic provisioning**: Auto-install via SSH on server add/update. Falls back to `~/.local/bin` if `/usr/local/bin` isn't writable
- **Snapshot caching**: In-memory TTL cache (60s for snapshot lists, 5m for repo stats) to avoid repeated S3 queries
- **Retention** (`app/retention.py` pure helpers, runners in `main.py`): off unless `RETENTION_ENABLED=true`. After a backup, on each storage that got the snapshot, restic runs *in the backupx container* (pinned static restic in the Dockerfile, not on the target host): `forget --dry-run --json --tag automated --group-by host,paths,tags` (DB jobs: `host`; their dump paths are random and anchored dumps carry per-dump position tags) with the job's `--keep-hourly/daily/weekly/monthly`; the plan is refused if any group would lose all its snapshots, then exactly those snapshot IDs are forgotten. Jobs with all keep fields 0 are never forgotten. A weekly `prune --max-unused` (`RETENTION_PRUNE_CRON`, default Sunday 04:00 scheduler TZ) runs on its own one-thread `maintenance` executor, one repository at a time, under the job's run lock (skipped if a backup holds it). Repositories shared by several jobs (same endpoint/bucket/prefix) are skipped. `RETENTION_DRY_RUN=true` only logs. Results go to the `retention_runs` table (latest per job/storage shown in the Jobs list), not `history`; failures notify like backup failures
- **Point-in-time recovery** (`app/pitr.py` pure decisions, `app/pitr_scripts.py` shell, `app/pitr_service.py` DB + runs; runbook `docs/PITR.md`): per database job (`pitr_enabled`, `pitr_interval`, `pitr_retention_days`, `pitr_client_image`). MySQL/MariaDB: backupx installs a systemd streamer on the job's server (`backupx-pitr-binlog@<job>`, `mysqlbinlog --raw --stop-never` into `/var/lib/backupx-pitr/<job>/spool`, creds in `/etc/backupx-pitr/<job>/my.cnf`), and every interval ships closed binlogs (+ a copy of the open one) with restic to the sibling repo `<prefix>-binlog` on every storage, deleting spool files only after all storages succeed. Ticks run on their own APScheduler executor `pitr` (`<job>__pitr`, lock `<job>:pitr`). The nightly mysqldump adds `--source-data=2`/`--master-data=2` (anchor; falls back to a plain dump on failure) and the dump snapshot gets `pitr-anchor binlog:<file>:<pos>` tags (so DB-job retention groups by `host` only). Chains break on numbering holes, streamer gap events and `server_uuid` changes; states ok/lagging/broken/unanchored alert only on transitions. The official mysql images have no mysqlbinlog: client image `percona/percona-server:<ver>`; `pitr_client_auto` resolves it from the server version and pins the digest (`pitr.effective_client_image`), client containers run `--read-only --cap-drop ALL`. A server major.minor change starts a new chain. The streamer runs mysqlbinlog with `--defaults-file` (host `[client]` options like MariaDB 10.3's `default-character-set` break it). PostgreSQL: WAL-G (v2.0.1 for glibc < 2.31 / PG < 12), backupx installs binary + env + archive script; config changes/restarts only via the P3 maintenance flow; an idle server (current WAL segment = last archived) is ok however old the last archive is, `archive_command` failures make it lagging. Restores are downloadable scripts into scratch containers, never in place. The legacy in-place snapshot restore into a database (`api_snapshot_restore_db`) is off unless `ALLOW_DIRECT_DB_RESTORE=true` and has no UI

- **Background tasks** (`app/tasks.py`, design + handler how-to in `docs/UX-OVERHAUL.md`): anything that can outlive a request (run now, file restore, snapshot ZIP, repo init, restic install, PITR ship-now) is a row in `tasks`; the route returns 202 with the task and a runner thread in some gunicorn worker claims it (`FOR UPDATE SKIP LOCKED`, own pool `TASK_MAX_CONCURRENT`), heartbeats, logs redacted lines to `task_logs` and parses `BACKUPX-STEP n/m text` markers into progress. Stale heartbeats are failed `interrupted`. Handlers take the usual advisory locks via `ctx.lock()` and mark restic repositories in use with shared `repo_locks()` (backups do too). The SPA's task tray polls `/api/tasks`. Params never hold secrets (redacted anyway); register secret values with `ctx.add_secret` so echoed output is masked
- **Per-storage run results**: every backup run has a `run_id`; `_finish_backup` writes `run_results` + redacted, capped `run_logs` per storage and `history.run_id`. `POST /api/jobs/:id/run {"storage_ids": [...]}` re-runs only some storages
- **CSRF / step-up / plans**: CSRF is enforced on every mutating route except login; the SPA sends `X-CSRFToken` (global fetch wrapper in `frontend/src/lib/api.ts`). Confirm/maintenance actions use `@requires_step_up` (password re-entered within `STEP_UP_TTL`) and `security.check_confirmed()` (server re-derives the plan; client echoes `plan_hash` + typed phrase)
- **SSH host keys**: ssh uses the writable `/app/data/known_hosts` (`-o UserKnownHostsFile=`, accept-new); the mounted `~/.ssh/known_hosts` is merged in at start-up
- **Health checks and fixes** (`app/health.py` pure detectors, `app/health_service.py` DB/probes/fixes; details in `docs/UX-OVERHAUL.md` P1): cheap detectors run per `GET /api/health` from the DB, expensive probes (SSH, restic cat config/locks/keys, weekly `restic check`, daily `restic stats`, PITR prereqs) run in an hourly `health_sweep` task queued by the leader (executor `health`) or on demand and are cached in `health_probes`; `health_findings` keeps first_seen/snooze/alert state. Each fix is a task kind `fix.<action>` with a tier (safe = one click and in "Fix all"; confirm = preview plan + typed phrase + step-up) and a `plan()` re-derived at execution. Stale restic locks are auto-removed only when older than 30 min, from a managed host or the backupx container, with no backupx advisory lock (checked read-only via `pg_locks`) and under `repo_locks(exclusive=True)`; `restic unlock` runs on the lock's host, never `--remove-all`. Alerts: critical only, one per issue + one when resolved (kinds already notified elsewhere have `alert=False`)
- **Enable-PITR wizard / maintenance flow** (`app/pitr_enable.py` pure, `app/maint_scripts.py` shell, `app/pitr_enable_service.py`; details in `docs/UX-OVERHAUL.md` P3): `/jobs/:id/pitr-setup`; `POST /pitr/plan` maps failing prerequisite checks to items (drop-in `zz-backupx-pitr.cnf` in the last-included conf.d, SET PERSIST / SET GLOBAL, GRANTs, ALTER SYSTEM reload vs restart, client image, `[client]` charset fix; docker only with a bind-mounted conf dir; managed/remote manual only); `POST /pitr/apply` (step-up + typed DB name + plan hash) runs task `pitr_enable`: Apply -> Restart (now or child `pitr_enable_restart` at `not_before`; fresh backup unless < 1 h, config backup, validate, restart, health check, rollback + alert; never rolls back during crash recovery) -> Stream (warm-up until the chain has the server uuid) -> Anchor -> state ok. Backupx may restart databases only through this flow
- **One-time credentials** (`app/credentials.py`): an admin login from a request lives only in that worker's memory keyed by the task id; the task is pinned (`tasks.pinned_worker`) to that worker, takes it at start and sends it only as a 0600 option file over ssh stdin; orphans fail after `CREDENTIAL_TTL`
- **Restore drills** (`app/drill.py`, `app/drill_service.py`): weekly `restore_drill_all` (`RESTORE_DRILL_CRON`, one job at a time) / `POST /api/jobs/:id/restore-drill`: backups only, never production (no SSH/SQL/PITR tick; enforced by `tests/test_drill.py`): scratch A = newest anchored dump / WAL-G base alone (`recovery_target='immediate'`), B = PITR to that backup's own point, A and B checksummed and must be identical; C = PITR to the latest point must apply cleanly and every table read; copies one at a time; result in `health_probes` `drill:<job>`, job card "Verified restore", alert + issue on failure. Per-job `restore_verify.drill_count_only_tables` = row counts instead of checksums (big log tables); not enough scratch disk (checked before reading production) = skipped with a warning, not failed `tests/test_prod_safety.py` enforces that backup/streaming/prereq/verify SQL only reads
- **Restore wizard** (`app/restore.py` pure, `app/restore_scripts.py` shell, `app/restore_service.py` rows/plans/tasks; details in `docs/UX-OVERHAUL.md` P2): `/restore` restores into a *scratch* copy on the backupx server first - restic/wal-g run in the container, the scratch DB is a host-docker container `backupx-restore-<id>` (label `backupx.restore=<id>`, `--network none`, no published ports, removed after `RESTORE_TTL_HOURS`) fed through `docker exec -i`/`docker cp`; a plan (dump/binlog/WAL-G/files, sizes, free disk vs estimated size x 2.5) is hash-confirmed. Replace production (maintenance tier: typed DB name + step-up + plan hash) = real `run_backup(tags=[pre-restore, restore:<id>])` (no `automated` tag, so retention never forgets it; expired after `PRE_RESTORE_KEEP_DAYS` by the cleanup tick) -> scratch dump (`--set-gtid-purged=OFF`, DEFINERs stripped off docker paths) -> rollback copy + import on the job server over its normal path -> verify; failures import the rollback copy (`rolled_back`). Undo = scratch restore of the pre-restore snapshot + the same flow. The image needs the docker CLI + socket group (`DOCKER_GID`)

### Data Flow
1. React UI calls Flask REST API (`/api/*`)
2. Flask stores job config in PostgreSQL with encrypted credentials (AES-256 via Fernet)
3. APScheduler triggers jobs at scheduled times
4. Jobs execute via SSH to remote servers (restic auto-provisioned if missing)
5. Remote servers run Restic backups to S3 storage
6. Audit logs recorded with sensitive field redaction

## Tech Stack

**Backend**: Flask 3.0, APScheduler, PostgreSQL, Redis (optional), Gunicorn
**Frontend**: React 19, TypeScript, Vite, Radix UI, Tailwind CSS, React Hook Form + Zod
**Backup Engine**: Restic with S3-compatible storage
**External Tools**: `ssh`, `sshpass`, `mysqldump`, `pg_dump`

## Environment Configuration

App config: `.env` (see `.env.example`)

Key variables:
- `SECRET_KEY` — Encryption key for stored credentials (min 32 chars, required)
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — Initial admin login (password min 12 chars in production)
- `DATABASE_PASSWORD` — Shared between app and postgres container
- `LISTEN_PORT` — Port gunicorn binds to (default 9090)
- `SESSION_TYPE` — `filesystem` (default) or `redis`
- `SCHEDULER_ENABLED` — `false` to keep an instance from ever running scheduled jobs (default `true`)
- `SCHEDULER_LOCK_INTERVAL` — seconds between scheduler lock checks / schedule syncs (default 15)
- `BACKUP_MAX_CONCURRENT` — scheduled backups running at once (default 4)
- `BACKUP_MISFIRE_GRACE` / `BACKUP_OVERDUE_GRACE` — seconds a queued run may start late / before a job is flagged overdue (default 21600 each)
- `DATABASE_POOL_MAX` / `DATABASE_POOL_TIMEOUT` — pooled connections per worker (default 20) / seconds to wait for one (default 30)
- `RETENTION_ENABLED` / `RETENTION_DRY_RUN` — run restic forget/prune at all (default `false`) / only log what would be removed (default `false`)
- `RETENTION_PRUNE_CRON` / `RETENTION_PRUNE_MAX_UNUSED` — weekly prune schedule in the scheduler timezone (default `0 4 * * sun`, standard crontab weekdays) / restic `--max-unused` (default `10%`)
- `RETENTION_LOCK_WAIT` / `RETENTION_FORGET_TIMEOUT` / `RETENTION_PRUNE_TIMEOUT` — restic `--retry-lock` (default `10m`) / seconds per forget (900) and prune (14400) run
- `TASK_RUNNER_ENABLED` / `TASK_MAX_CONCURRENT` / `TASK_POLL_INTERVAL` / `TASK_STALE_AFTER` / `TASK_KEEP_DAYS` — run background tasks in this instance (default `true`) / task threads per worker (4) / seconds between claims (2) / heartbeat age that counts as interrupted (90) / days finished tasks are kept (30)
- `STEP_UP_TTL` / `RUN_RESULTS_KEEP_DAYS` / `RESTORE_TIMEOUT` / `SSH_KNOWN_HOSTS_FILE` — seconds a password re-entry counts (300) / days per-storage run results are kept (90) / seconds per file restore task (21600) / writable known_hosts (default `/app/data/known_hosts`)
- `PITR_MAX_CONCURRENT` / `PITR_LAG_MINUTES` / `PITR_SHIP_TIMEOUT` / `PITR_HEARTBEAT_MAX_AGE` — threads of the `pitr` executor (2) / lag before "lagging" (0 = max(3 x interval, 15)) / seconds per shipment (900) / streamer heartbeat age that counts as stalled (300)
- `HEALTH_SWEEP_MINUTES` / `HEALTH_ALERT_INTERVAL` / `HEALTH_CHECK_DAYS` / `HEALTH_STATS_HOURS` — health sweep interval (60) / alert tick seconds (300) / `restic check` every N days (7) / `restic stats` every N hours (24)
- `HEALTH_ALERT_CHANNELS` / `STALE_LOCK_MIN_AGE` / `FIX_RETRY_BACKOFF` — channel types that get health alerts (`telegram`) / seconds before a restic lock counts as stale (1800) / seconds between storage retry attempts (`30,120`)
- `RESTORE_TTL_HOURS` / `RESTORE_MAX_KEEP_HOURS` / `PRE_RESTORE_KEEP_DAYS` - scratch copies live 24 h / at most 168 h when extended / pre-restore snapshots kept 30 days (forgotten only with `RETENTION_ENABLED`)
- `RESTORE_WORK_DIR` / `RESTORE_DISK_FACTOR` / `RESTORE_DUMP_EXPANSION` - work dir (`/app/data/restores`) / free disk needed = estimated restored size x 2.5 / a gzipped dump restores to ~5x its size
- `RESTORE_DOCKER_NETWORK` / `RESTORE_HELPER_IMAGE` / `RESTORE_HOST_FILES_DIR` / `RESTORE_CLEANUP_MINUTES` / `DOCKER_GID` - network for WAL-G scratch servers (default bridge) / helper for file extracts (`busybox:1.36`) / extract root on the backupx host (`/var/tmp/backupx-restore`) / cleanup tick (10) / docker socket group for the backupx user (compose `group_add`)
- `PITR_MAINT_HEALTH_TIMEOUT` / `PITR_MAINT_RECOVERY_MAX_WAIT` / `PITR_FRESH_BACKUP_MAX_AGE` / `PITR_PLAN_CACHE_SECONDS` - restarted DB healthy within 300 s / never roll back during recovery, give up after 7200 s / backup newer than 3600 s skips the fresh backup / wizard check cached 900 s
- `PITR_BINLOG_BASENAME` / `PITR_BINLOG_EXPIRE_DAYS` / `PITR_MANAGED_HOSTS` / `CREDENTIAL_TTL` / `PITR_ENABLE_OK_WAIT` - `mysql-bin` / 2 / extra managed-host regex / 900 s / 600 s
- `RESTORE_DRILL_ENABLED` / `RESTORE_DRILL_CRON` / `RESTORE_DRILL_MAX_TABLE_ROWS` / `RESTORE_DRILL_MAX_TABLES` - weekly drills on (true) / `30 3 * * 6` / 5 M rows (larger: count only) / 2000
- `ALLOW_DIRECT_DB_RESTORE` — `true` re-enables `POST /api/jobs/<id>/snapshots/<snap>/restore-db` (default `false`: HTTP 403). It pipes a dump into the job's live database with no pre-restore backup or server-side confirmation; the UI no longer offers it; the restore wizard (scratch first, gated replace-production) replaces it. Scratch restores take `compare_production` (default false, wizard checkbox unticked; part of the plan and its hash): only an explicit true makes the Verify step and Re-check read production row counts; otherwise they count rows in the copy only (Undo's scratch copy still compares, it replaces production anyway). Dashboard "Running (N)" counts jobs with a backup, a restore drill or a person's scratch restore/replace/undo running (`job_activity()`, card field `activity`); ready scratch copies don't count

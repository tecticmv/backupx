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
- **Point-in-time recovery** (`app/pitr.py` pure decisions, `app/pitr_scripts.py` shell, `app/pitr_service.py` DB + runs; runbook `docs/PITR.md`): per database job (`pitr_enabled`, `pitr_interval`, `pitr_retention_days`, `pitr_client_image`). MySQL/MariaDB: backupx installs a systemd streamer on the job's server (`backupx-pitr-binlog@<job>`, `mysqlbinlog --raw --stop-never` into `/var/lib/backupx-pitr/<job>/spool`, creds in `/etc/backupx-pitr/<job>/my.cnf`), and every interval ships closed binlogs (+ a copy of the open one) with restic to the sibling repo `<prefix>-binlog` on every storage, deleting spool files only after all storages succeed. Ticks run on their own APScheduler executor `pitr` (`<job>__pitr`, lock `<job>:pitr`). The nightly mysqldump adds `--source-data=2`/`--master-data=2` (anchor; falls back to a plain dump on failure) and the dump snapshot gets `pitr-anchor binlog:<file>:<pos>` tags (so DB-job retention groups by `host` only). Chains break on numbering holes, streamer gap events and `server_uuid` changes; states ok/lagging/broken/unanchored alert only on transitions. The official mysql images have no mysqlbinlog: client image `percona/percona-server:<ver>`. PostgreSQL: WAL-G (v2.0.1 for glibc < 2.31 / PG < 12), backupx installs binary + env + archive script and prints postgresql.conf changes, never restarts. Restores are downloadable scripts into scratch containers, never in place

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
- `PITR_MAX_CONCURRENT` / `PITR_LAG_MINUTES` / `PITR_SHIP_TIMEOUT` / `PITR_HEARTBEAT_MAX_AGE` — threads of the `pitr` executor (2) / lag before "lagging" (0 = max(3 x interval, 15)) / seconds per shipment (900) / streamer heartbeat age that counts as stalled (300)

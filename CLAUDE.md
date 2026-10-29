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
- `app/scheduler/distributed.py` - Older leader-election scheduler, not wired up (its tests are stale)
- `app/session.py` - Session management (filesystem or Redis)
- `frontend/` - React + TypeScript + Vite + Radix UI + Tailwind
- `tests/` - Pytest test suite
- `Dockerfile` - Multi-stage build (Node for frontend, Python for backend)
- `docker-compose.yml` - Main stack (backup-ui + postgres)
- `docker-compose.host.yml` - Override for Linux host networking

### Key Patterns
- **Database**: Factory pattern — all DB operations go through `DatabaseBackend` interface
- **SSH auth**: Three modes supported — key file path, pasted/uploaded key content, password (via `sshpass -e`, password in `$SSHPASS`). Sensitive values encrypted with Fernet
- **No secrets in argv**: argv is world-readable (`ps`, `/proc/PID/cmdline`). Remote scripts go over stdin via `_ssh_run` / `_ssh_popen` (ssh runs only the fixed `REMOTE_SHELL`, `bash -s`); never `ssh_cmd + [script]`. Inside scripts, secrets are `export`ed env vars (restic, AWS_*, PGPASSWORD) or a 0600 MySQL option file (`_mysql_option_file` + `--defaults-extra-file`), never `-p<password>` or `docker exec -e VAR=value`. Local restic/rclone calls pass secrets via `env=`
- **Notifications**: Telegram text is HTML-escaped (only our `<b>` is markup), cut to 4096 without splitting entities, and resent as plain text on HTTP 400; Slack text escapes `& < >`; code blocks can't be closed by message text
- **Sessions**: Strategy pattern — filesystem (default) or Redis for horizontal scaling
- **Scheduler**: Every gunicorn worker imports the app, but only the one holding a Postgres advisory lock starts APScheduler; it re-reads jobs/timezone from the DB every `SCHEDULER_LOCK_INTERVAL` (15s), and another worker takes over if it dies. Each backup run holds a per-job advisory lock, so a job never runs twice at once (a second trigger is skipped and logged, no notification)
- **Backup runs and the DB pool**: At most `BACKUP_MAX_CONCURRENT` scheduled runs at once; the rest queue (misfire grace `BACKUP_MISFIRE_GRACE`, so queued runs aren't dropped). Advisory locks use dedicated non-pooled connections; a run borrows a pooled connection per query (`short_lived_connections()` mode) instead of holding one for hours. The pool waits up to `DATABASE_POOL_TIMEOUT` for a free connection instead of raising at once. `run_backup` never raises: any error marks the job failed, writes history and notifies; an APScheduler error/missed listener is the last resort
- **Overdue jobs**: `/api/jobs` and `/api/dashboard/stats` flag scheduled jobs that haven't started a run since the last expected fire time (`BACKUP_OVERDUE_GRACE` ago), computed with APScheduler's CronTrigger in the scheduler timezone
- **Restic init**: The backup script runs `restic init` only when `restic cat config` reports the repo doesn't exist (exit 10, or the old-restic message); any other open error fails that storage
- **Restic provisioning**: Auto-install via SSH on server add/update. Falls back to `~/.local/bin` if `/usr/local/bin` isn't writable
- **Snapshot caching**: In-memory TTL cache (60s for snapshot lists, 5m for repo stats) to avoid repeated S3 queries

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

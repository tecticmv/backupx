# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BackupX is an enterprise backup management system with a Flask backend and React frontend. It manages filesystem and database backups across multiple servers using Restic and S3-compatible storage.

## Commands

All commands use `./run.sh` from the project root:

```bash
# Development
./run.sh server:dev          # Start server with hot reload
./run.sh agent:dev           # Start agent with hot reload

# Production
./run.sh server:start        # Start server (gunicorn, 4 workers)
./run.sh agent:start         # Start agent
./run.sh server:stop         # Stop server
./run.sh server:status       # Check server status

# Testing
./run.sh test                           # Run all tests
./run.sh test:cov                       # Run tests with coverage
./run.sh test tests/test_security.py    # Run specific test file
./run.sh test tests/test_security.py::TestAuthentication::test_login_success  # Run single test

# Other
./run.sh install             # Install all dependencies
./run.sh check               # Pre-deployment checks (Python version, env, deps, tests)
./run.sh logs [server|agent] # Tail logs
```

Frontend build (from `backupx-server/frontend/`):
```bash
npm run dev      # Development server
npm run build    # Production build
npm run lint     # ESLint
```

## Architecture

### Two-Component System
- **Server** (`backupx-server/`): Flask app with React UI, job scheduling, and configuration storage
- **Agent** (`backupx-agent/`): Optional lightweight Flask agent deployed on remote servers

### Server Structure (`backupx-server/`)
- `app/main.py` - Monolithic Flask app containing all REST API routes (~40 endpoints under `/api/*`)
- `app/db/` - Database abstraction layer (SQLite default, PostgreSQL optional)
  - `base.py` - Abstract `DatabaseBackend` interface
  - `sqlite.py` / `postgres.py` - Implementations
  - `factory.py` - `create_database_backend()` factory function
  - `migrate.py` - Schema migrations
- `app/audit/` - Audit logging with `@audit_log` decorator
- `app/scheduler/distributed.py` - APScheduler with database-backed distributed coordination
- `app/session.py` - Session management (filesystem or Redis backends)
- `frontend/` - React + TypeScript + Vite + Radix UI + Tailwind
- `tests/` - Pytest test suite

### Agent Structure (`backupx-agent/`)
- `agent.py` - Minimal Flask app with HMAC-SHA256 authentication

### Key Patterns
- **Database**: Abstract factory pattern - all DB operations go through `DatabaseBackend` interface
- **Audit**: Decorator pattern - use `@audit_log` to track operations with automatic sensitive field redaction
- **Sessions**: Strategy pattern - filesystem (default) or Redis for horizontal scaling
- **Scheduler**: Hybrid APScheduler + database for distributed deployments with leader election

### Data Flow
1. React UI calls Flask REST API (`/api/*`)
2. Flask stores job config in database with encrypted credentials (AES-256 via Fernet)
3. APScheduler triggers jobs at scheduled times
4. Jobs execute via SSH or Agent REST calls to remote servers
5. Remote servers run Restic backups to S3 storage
6. Audit logs recorded with sensitive field redaction

## Tech Stack

**Backend**: Flask 3.0, APScheduler, SQLite/PostgreSQL, Redis (optional), Gunicorn
**Frontend**: React 19, TypeScript, Vite, Radix UI, Tailwind CSS, React Hook Form + Zod
**Backup Engine**: Restic with S3-compatible storage
**External Tools**: mysqldump, pg_dump (for database backups)

## Environment Configuration

Server config: `backupx-server/.env` (see `.env.example`)
Agent config: `backupx-agent/.env` (see `.env.example`)

Key variables:
- `SECRET_KEY` - Encryption key (min 32 chars, required)
- `DATABASE_TYPE` - `sqlite` or `postgresql`
- `SESSION_TYPE` - `filesystem` or `redis`
- `AGENT_API_KEY` - Agent authentication key (min 32 chars)

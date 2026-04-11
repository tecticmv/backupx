# BackupX

A modern backup management system with a web UI. Orchestrates [Restic](https://restic.net/) backups across multiple servers to S3-compatible storage — all over SSH, no agents required.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11-green.svg)
![React](https://img.shields.io/badge/react-19-61dafb.svg)

## Features

- **Filesystem & MySQL backups** to any S3-compatible target (AWS S3, MinIO, Backblaze B2, Wasabi)
- **Agentless** — connects to remote servers via SSH, auto-installs restic on first connection
- **Flexible SSH auth** — key file path, pasted/uploaded key, or password (credentials encrypted at rest)
- **Remote directory browser** — pick backup paths visually instead of typing them
- **Cron-based scheduling** with timezone support
- **Retention policies** — hourly / daily / weekly / monthly
- **Snapshot browser & one-click restore** via SSH
- **Audit log** of every administrative action
- **Encryption** — AES-256 (Fernet) for stored credentials, restic handles on-disk encryption

## Quick Start

```bash
git clone https://github.com/SaiphMuhammad/backupx.git
cd backupx
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY, ADMIN_PASSWORD, DATABASE_PASSWORD
docker compose up -d
```

Then open `http://localhost:9090` and log in with the credentials from your `.env`.

## Deployment Modes

### Bridge network (default)

Works everywhere (Docker Desktop on Windows/Mac, Linux). Web UI is published on the configured port.

```bash
docker compose up -d
```

### Host network (Linux only)

Required when running on a Linux host where the container needs to reach LAN servers on the same subnet (avoids Docker NAT collisions).

```bash
docker compose -f docker-compose.yml -f docker-compose.host.yml up -d
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Fernet key for credential encryption | **Required** (min 32 chars) |
| `ADMIN_USERNAME` | Initial admin username | **Required** (not `admin`) |
| `ADMIN_PASSWORD` | Initial admin password | **Required** (min 12 chars) |
| `DATABASE_PASSWORD` | Shared between backup-ui and postgres | **Required** |
| `LISTEN_PORT` | Port gunicorn binds to | `9090` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` |
| `TZ` | Timezone | `UTC` |
| `SESSION_TYPE` | `filesystem` or `redis` | `filesystem` |
| `SCHEDULER_MODE` | `standalone` or `distributed` | `standalone` |

See `.env.example` for the full list.

## Usage

1. **Add a server** — Settings → Servers. Enter host, SSH user, and choose auth method (key path / pasted key / password). BackupX will test the connection and auto-install restic.
2. **Add S3 storage** — Settings → S3 Storage. Point it at MinIO, AWS, etc.
3. **Create a backup job** — Jobs → New. Pick the server, browse directories, set S3 target, restic password, schedule, and retention.
4. **View snapshots** — click a job to see its snapshots. Use the restore button to pull files back to the source server.

## Architecture

```
┌──────────────────────────────────────────┐
│            BackupX (Docker)              │
│  ┌────────────┐  ┌────────────────────┐  │
│  │  React UI  │  │  Flask API + APS   │  │
│  └────────────┘  └────────────────────┘  │
│              │                           │
│              ▼                           │
│        ┌──────────┐                      │
│        │ Postgres │ (sidecar container)  │
│        └──────────┘                      │
└──────────────────────────────────────────┘
                    │
                    │  SSH
                    ▼
          ┌───────────────────┐
          │   Remote Server   │
          │  (runs restic)    │
          └─────────┬─────────┘
                    │
                    │  S3 API
                    ▼
          ┌───────────────────┐
          │  MinIO / S3 etc.  │
          └───────────────────┘
```

## Recovery Notes

- **Losing the restic password means losing the backups** — there is no recovery path. Store it somewhere outside BackupX.
- BackupX stores the restic password encrypted in PostgreSQL using your `SECRET_KEY`. You can reveal it from the job edit dialog (audit-logged).
- Back up your `SECRET_KEY` along with the postgres volume — together they're everything you need to restore the BackupX state.

## License

MIT — see [LICENSE](LICENSE).

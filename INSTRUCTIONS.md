# BackupX Setup Instructions

## Prerequisites

- Python 3.9+
- Restic (for backup operations)
- MySQL client (for MySQL database backups)
- PostgreSQL client (for PostgreSQL database backups)

## Quick Start

```bash
# 1. Install dependencies
./run.sh install

# 2. Configure environment
cp backupx-server/.env.example backupx-server/.env
# Edit .env with your settings (see Configuration below)

# 3. Start in development mode
./run.sh server:dev

# 4. Access the web UI
open http://localhost:5000
```

## Configuration

### Server Configuration (backupx-server/.env)

```bash
# REQUIRED - Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=your-secret-key-min-32-chars

# REQUIRED - Minimum 12 characters
ADMIN_USERNAME=your-admin-username
ADMIN_PASSWORD=your-secure-password-min-12-chars

# Optional
DATABASE_PATH=data/backupx.db
LOG_LEVEL=INFO
```

### Agent Configuration (backupx-agent/.env)

```bash
# REQUIRED - Minimum 32 characters
AGENT_API_KEY=your-api-key-min-32-chars
AGENT_PORT=8090
```

## Production Deployment

### Security Requirements

Production mode enforces these security requirements:
- SECRET_KEY: minimum 32 characters, not default value
- ADMIN_PASSWORD: minimum 12 characters, not weak values (changeme, admin, password)
- AGENT_API_KEY: minimum 32 characters

### Start Production Server

```bash
# Run pre-deployment checks
./run.sh check

# Start server with gunicorn
./run.sh server:start

# Start agent
./run.sh agent:start
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| SERVER_PORT | 5000 | Server listening port |
| SERVER_WORKERS | 4 | Gunicorn worker processes |
| SERVER_THREADS | 2 | Threads per worker |
| AGENT_PORT | 8090 | Agent listening port |
| DEBUG | 0 | Enable debug output |

## Commands Reference

| Command | Description |
|---------|-------------|
| `./run.sh server:start` | Start server (production mode with gunicorn) |
| `./run.sh server:dev` | Start server (development mode) |
| `./run.sh server:stop` | Stop server |
| `./run.sh server:restart` | Restart server |
| `./run.sh server:status` | Check server status |
| `./run.sh agent:start` | Start agent (production mode) |
| `./run.sh agent:dev` | Start agent (development mode) |
| `./run.sh agent:stop` | Stop agent |
| `./run.sh agent:restart` | Restart agent |
| `./run.sh agent:status` | Check agent status |
| `./run.sh test` | Run tests |
| `./run.sh test:cov` | Run tests with coverage |
| `./run.sh install` | Install dependencies |
| `./run.sh check` | Run pre-deployment checks |
| `./run.sh logs [server\|agent]` | Tail logs |

## Architecture

```
   BACKUP SERVER                    REMOTE SERVERS
┌─────────────────┐            ┌─────────────────────┐
│  BackupX Server │            │   BackupX Agent     │
│    (Flask)      │── REST ───>│   (optional)        │
│                 │            └─────────────────────┘
│                 │            ┌─────────────────────┐
│                 │── SSH ────>│   Remote Host       │
│                 │            │   (no agent)        │
└─────────────────┘            └─────────────────────┘
        │
        v
   SQLite DB
   S3 Storage
```

### Deployment Model

- **BackupX Server**: Runs on your central backup server. This is where you manage all backup jobs and store configuration.
- **BackupX Agent**: Optionally deployed on remote servers you want to back up. Provides better performance for database backups.

### Connection Types

1. **SSH Connection**: Server connects directly to remote hosts via SSH (no agent required)
2. **Agent Connection**: Server communicates with BackupX Agent installed on remote hosts (better for database backups)

### Backup Types

- **File backups**: Uses Restic for incremental, encrypted backups
- **MySQL backups**: Uses mysqldump with compression
- **PostgreSQL backups**: Uses pg_dump with compression
# BackupX

A modern, enterprise-ready backup management system with a beautiful web UI. Manage filesystem and database backups across multiple servers using [Restic](https://restic.net/) and S3-compatible storage.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9+-green.svg)
![React](https://img.shields.io/badge/react-18+-61dafb.svg)

## Features

- **Multi-Server Backup Management** - Manage backups across multiple servers from a single dashboard
- **Two Connection Modes**:
  - **SSH** - Connect directly via SSH (no agent required)
  - **Agent** - Lightweight agent for secure, firewall-friendly backups
- **Backup Types**:
  - Filesystem backups with include/exclude patterns
  - MySQL database backups
- **S3-Compatible Storage** - Works with AWS S3, MinIO, Backblaze B2, Wasabi, etc.
- **Scheduling** - Cron-based scheduling with timezone support
- **Notifications** - Email, Slack, Discord, and webhook notifications
- **Enterprise Features**:
  - PostgreSQL support for high availability
  - Audit logging with sensitive field redaction
  - Redis session storage for horizontal scaling
  - Distributed scheduler with leader election
- **Security**:
  - AES-256 credential encryption
  - Rate limiting and CSRF protection
  - Security headers (CSP, HSTS, etc.)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      BackupX Server                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   React UI   │  │  Flask API   │  │  Scheduler (APSched) │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
              │                              │
              │ HTTPS                        │ HTTPS
              ▼                              ▼
┌─────────────────────┐          ┌─────────────────────┐
│   Remote Server     │          │   Remote Server     │
│   (SSH Connection)  │          │   (BackupX Agent)   │
└─────────────────────┘          └─────────────────────┘
              │                              │
              └──────────────┬───────────────┘
                             ▼
                   ┌─────────────────┐
                   │   S3 Storage    │
                   │  (Restic Repo)  │
                   └─────────────────┘
```

## Quick Start

### Interactive Setup (Recommended)

```bash
git clone https://github.com/SaiphMuhammad/backupx.git
cd backupx
chmod +x run.sh
./run.sh
```

The interactive wizard will guide you through setting up the server or agent.

### Manual Setup

#### Server

```bash
cd backupx-server

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your settings (especially SECRET_KEY and ADMIN_PASSWORD)

# Build frontend
cd frontend
npm install
npm run build
cd ..

# Run (development)
flask --app app.main run --host 0.0.0.0 --port 5000

# Run (production)
gunicorn --bind 0.0.0.0:5000 --workers 4 app.main:app
```

#### Agent

```bash
cd backupx-agent

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env - set AGENT_API_KEY (min 32 chars)

# Run
python agent.py
```

### Docker

#### Server

```bash
cd backupx-server
cp .env.example .env
# Edit .env with your settings

# Build frontend first
cd frontend && npm install && npm run build && cd ..

docker compose up -d
```

#### Agent

```bash
cd backupx-agent
cp .env.example .env
# Edit .env - set AGENT_API_KEY

docker compose up -d
```

## Configuration

### Server Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Encryption key (min 32 chars) | **Required** |
| `ADMIN_USERNAME` | Admin login username | `admin` |
| `ADMIN_PASSWORD` | Admin login password | **Required** |
| `DATABASE_TYPE` | `sqlite` or `postgresql` | `sqlite` |
| `DATABASE_PATH` | SQLite database path | `data/backupx.db` |
| `AUDIT_ENABLED` | Enable audit logging | `true` |
| `SESSION_TYPE` | `filesystem` or `redis` | `filesystem` |
| `REDIS_HOST` | Redis host (if using Redis) | `localhost` |

See [backupx-server/.env.example](backupx-server/.env.example) for all options.

### Agent Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_API_KEY` | API key for authentication (min 32 chars) | **Required** |
| `AGENT_NAME` | Display name in UI | `backupx-agent` |
| `AGENT_PORT` | Port to listen on | `8090` |
| `ALLOWED_PATHS` | Comma-separated allowed backup paths | (all) |
| `LOG_LEVEL` | Logging level | `INFO` |

## Usage

### Adding a Server

1. Go to **Servers** in the sidebar
2. Click **Add Server**
3. Choose connection type:
   - **SSH**: Enter hostname, port, username, and SSH key/password
   - **Agent**: Enter hostname, port, and API key
4. Test connection and save

### Creating a Backup Job

1. Go to **Backup Jobs**
2. Click **Add Job**
3. Configure:
   - Select server and S3 storage
   - Choose backup type (filesystem or database)
   - Set paths/databases to backup
   - Configure schedule (cron expression)
   - Set retention policy
4. Save and optionally run immediately

### Viewing Snapshots

1. Go to **Backup Jobs**
2. Click the snapshots icon on a job
3. View all snapshots with timestamps and sizes
4. Restore or delete snapshots as needed

## CLI Commands

```bash
./run.sh                    # Interactive setup wizard
./run.sh server:start       # Start server (production)
./run.sh server:dev         # Start server (development)
./run.sh server:stop        # Stop server
./run.sh server:status      # Check server status
./run.sh agent:start        # Start agent (production)
./run.sh agent:dev          # Start agent (development)
./run.sh agent:stop         # Stop agent
./run.sh test               # Run tests
./run.sh test:cov           # Run tests with coverage
./run.sh install            # Install all dependencies
./run.sh check              # Pre-deployment checks
./run.sh logs [server|agent] # Tail logs
./run.sh help               # Show all commands
```

## Production Deployment

### Security Checklist

- [ ] Generate strong `SECRET_KEY`: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- [ ] Set strong `ADMIN_PASSWORD` (min 12 chars)
- [ ] Use HTTPS with a reverse proxy (nginx, Caddy, Traefik)
- [ ] Configure firewall rules
- [ ] Enable audit logging
- [ ] Regular database backups

### Reverse Proxy (nginx)

```nginx
server {
    listen 443 ssl http2;
    server_name backupx.example.com;

    ssl_certificate /etc/letsencrypt/live/backupx.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/backupx.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### High Availability Setup

For enterprise deployments:

```bash
# Use PostgreSQL
DATABASE_TYPE=postgresql
DATABASE_HOST=postgres.example.com
DATABASE_NAME=backupx
DATABASE_USER=backupx
DATABASE_PASSWORD=secure-password

# Use Redis for sessions
SESSION_TYPE=redis
REDIS_HOST=redis.example.com

# Enable distributed scheduler
SCHEDULER_MODE=distributed
```

## API Reference

The server exposes a REST API at `/api/`. All endpoints require authentication.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/login` | POST | Login |
| `/api/auth/logout` | POST | Logout |
| `/api/jobs` | GET/POST | List/create jobs |
| `/api/jobs/<id>` | GET/PUT/DELETE | Get/update/delete job |
| `/api/jobs/<id>/run` | POST | Run job manually |
| `/api/servers` | GET/POST | List/create servers |
| `/api/s3-configs` | GET/POST | List/create S3 configs |
| `/api/history` | GET | Backup history |
| `/api/audit` | GET | Audit logs |

## Development

### Project Structure

```
backupx/
├── backupx-server/          # Server application
│   ├── app/
│   │   ├── main.py          # Flask application
│   │   ├── db/              # Database abstraction
│   │   ├── audit/           # Audit logging
│   │   ├── scheduler/       # Distributed scheduler
│   │   └── session.py       # Session management
│   ├── frontend/            # React frontend
│   │   └── src/
│   │       ├── pages/       # Page components
│   │       └── components/  # UI components
│   └── tests/               # Test suite
├── backupx-agent/           # Agent application
│   └── agent.py             # Agent server
├── run.sh                   # CLI runner
└── README.md
```

### Running Tests

```bash
./run.sh test               # Run all tests
./run.sh test:cov           # With coverage report
./run.sh test -k test_auth  # Run specific tests
```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests
5. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) for details.

## Support

- **Issues**: [GitHub Issues](https://github.com/SaiphMuhammad/backupx/issues)
- **Discussions**: [GitHub Discussions](https://github.com/SaiphMuhammad/backupx/discussions)

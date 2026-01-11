# Backup Manager UI

A web interface for managing restic backups from remote servers to S3-compatible storage.

## Features

- Web-based backup job management
- Schedule backups with cron expressions
- View snapshots and repository statistics
- Backup history and status tracking
- Secure credential storage
- Dark theme UI

## Quick Start

1. **Clone and configure:**
   ```bash
   cd backup-ui
   cp .env.example .env
   # Edit .env with your settings
   ```

2. **Start the container:**
   ```bash
   docker compose up -d
   ```

3. **Access the UI:**
   Open http://localhost:8088 in your browser

4. **Login:**
   - Username: `admin` (or your configured username)
   - Password: `changeme` (or your configured password)

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Session encryption key | `change-this-secret-key` |
| `ADMIN_USERNAME` | Admin login username | `admin` |
| `ADMIN_PASSWORD` | Admin login password | `changeme` |
| `TZ` | Timezone | `UTC` |

### SSH Keys

Mount your SSH keys to allow the container to connect to remote servers:

```yaml
volumes:
  - ~/.ssh:/root/.ssh:ro
```

## Creating a Backup Job

1. Click **Jobs** → **New Job**
2. Fill in the configuration:
   - **Remote Host**: `user@server-ip`
   - **Directories**: Paths to backup (one per line)
   - **S3 Configuration**: Your Qumulo/S3 details
   - **Restic Password**: Encryption password (save this!)
3. Enable scheduling if desired
4. Click **Create Job**

## Restore from Backup

From any server with restic installed:

```bash
# Set environment
export AWS_ACCESS_KEY_ID='your-key'
export AWS_SECRET_ACCESS_KEY='your-secret'
export RESTIC_PASSWORD='your-password'
export RESTIC_REPOSITORY='s3:https://your-endpoint/bucket/prefix'

# List snapshots
restic snapshots

# Restore latest
restic restore latest --target /restore

# Restore specific snapshot
restic restore abc123 --target /restore

# Restore specific path
restic restore latest --target /restore --include "/var/www"
```

## Security Notes

- Change the default admin password immediately
- Use a strong `SECRET_KEY` in production
- The restic password is stored encrypted - save it securely elsewhere
- SSH keys are mounted read-only

## Architecture

```
┌─────────────────┐     SSH      ┌─────────────────┐
│   Backup UI     │ ──────────►  │  Remote Server  │
│   (Docker)      │              │  (restic runs)  │
└─────────────────┘              └────────┬────────┘
                                          │
                                          │ S3
                                          ▼
                                 ┌─────────────────┐
                                 │   Qumulo S3     │
                                 │   (storage)     │
                                 └─────────────────┘
```

## License

MIT

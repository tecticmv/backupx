# BackupX Deployment Guide

This guide covers deploying BackupX on a standalone server to manage backups of remote servers, databases, and S3 storage.

## Prerequisites

- Docker and Docker Compose installed on the deployment server
- SSH key pair for connecting to remote backup targets
- Network access to:
  - Remote servers (SSH port, typically 22)
  - MySQL databases (port 3306)
  - S3-compatible storage endpoints (HTTPS port 443)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/SaiphMuhammad/backupx.git
cd backupx

# Create environment file
cp .env.example .env

# Edit environment variables
nano .env

# Start the application
docker compose up -d
```

Access the web interface at `http://your-server-ip:8088`

## Configuration

### Environment Variables

Create a `.env` file with the following variables:

```bash
# REQUIRED: Generate a secure random key
# Use: openssl rand -hex 32
SECRET_KEY=your-secure-random-key-here

# Admin credentials
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password

# Logging level (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO

# Timezone
TZ=UTC
```

### SSH Keys Setup

BackupX needs SSH access to remote servers for backup operations.

1. **Generate SSH key pair** (if you don't have one):
   ```bash
   ssh-keygen -t ed25519 -C "backupx@your-server"
   ```

2. **Copy public key to remote servers**:
   ```bash
   ssh-copy-id -i ~/.ssh/id_ed25519.pub user@remote-server
   ```

3. **Verify the key location**:
   The default SSH key path in BackupX is `/home/backupx/.ssh/id_rsa`. The docker-compose.yml mounts your `~/.ssh` directory to this location.

   If your key has a different name, update the SSH key path when adding servers in the UI.

### Directory Structure

```
backupx/
├── config/          # Application configuration (mounted volume)
├── logs/            # Application logs (mounted volume)
├── data/            # SQLite database (Docker volume)
├── docker-compose.yml
├── .env
└── ...
```

## Production Deployment

### 1. Secure the Server

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Configure firewall
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 8088/tcp    # BackupX (or your chosen port)
sudo ufw enable
```

### 2. Configure Docker Compose for Production

Edit `docker-compose.yml` for your environment:

```yaml
version: "3.8"

services:
  backup-ui:
    build: .
    container_name: backup-ui
    restart: unless-stopped
    ports:
      - "8088:5000"  # Change port if needed
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - backup-data:/app/data
      # Mount your SSH keys
      - /path/to/your/.ssh:/home/backupx/.ssh:ro
    environment:
      - SECRET_KEY=${SECRET_KEY}
      - ADMIN_USERNAME=${ADMIN_USERNAME}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - TZ=${TZ:-UTC}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  backup-data:
```

### 3. Set Up Reverse Proxy (Recommended)

For HTTPS support, use Nginx as a reverse proxy:

```nginx
server {
    listen 80;
    server_name backupx.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name backupx.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/backupx.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/backupx.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Get SSL certificate with Let's Encrypt:
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d backupx.yourdomain.com
```

### 4. Start the Application

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Check status
docker compose ps
```

## Initial Setup in Web UI

1. **Login** with your admin credentials

2. **Add Remote Servers** (Configuration > Servers)
   - Name: Descriptive name
   - Host: IP or hostname
   - SSH User: User with backup permissions
   - SSH Port: Usually 22
   - SSH Key Path: `/home/backupx/.ssh/id_rsa` (or your key)

3. **Add S3 Storage** (Configuration > S3 Storage)
   - Name: Descriptive name
   - Endpoint: e.g., `s3.amazonaws.com` or `minio.example.com:9000`
   - Bucket: Your backup bucket name
   - Access Key / Secret Key: S3 credentials
   - Region: e.g., `us-east-1` (optional for some providers)

4. **Add Database Configs** (Configuration > Databases) - Optional
   - For MySQL database backups
   - Requires MySQL client on remote servers

5. **Create Backup Jobs** (Backup Jobs > Create Job)
   - Select job type (Filesystem or Database)
   - Choose server and storage
   - Configure directories/databases to backup
   - Set schedule (cron format)
   - Configure retention policy

6. **Set Up Notifications** (Configuration > Notifications) - Optional
   - Email (SMTP)
   - Slack webhook
   - Discord webhook
   - Generic webhook

## Connection Methods

BackupX supports two methods to connect to remote servers:

### Method 1: SSH Connection (Traditional)

Requires SSH key authentication from the BackupX server to remote servers.

**Pros:**
- No additional software on remote servers (only restic required)
- Works with existing SSH infrastructure

**Cons:**
- Requires SSH key management
- BackupX server needs network access to remote SSH ports
- Some cloud environments restrict SSH access

### Method 2: BackupX Agent (Recommended)

Deploy a lightweight agent on remote servers that communicates with the main BackupX server via HTTP API.

**Pros:**
- No SSH key management required
- Agent handles backup execution locally
- Better for cloud/containerized environments
- API key authentication

**Cons:**
- Requires deploying agent on each remote server

## Deploying BackupX Agent

Deploy the agent on each remote server you want to backup.

### Agent Quick Start

```bash
# On each remote server
cd /opt
git clone https://github.com/SaiphMuhammad/backupx.git
cd backupx/agent

# Create environment file
cp .env.example .env

# Generate API key
API_KEY=$(openssl rand -hex 32)
echo "AGENT_API_KEY=$API_KEY" >> .env
echo "AGENT_NAME=my-server-$(hostname)" >> .env

# Optional: Restrict backup paths
echo "ALLOWED_PATHS=/var/www,/home,/etc" >> .env

# Start the agent
docker compose up -d
```

### Agent Configuration

Edit `/opt/backupx/agent/.env`:

```bash
# REQUIRED: API key (must match in BackupX UI)
AGENT_API_KEY=your-secure-api-key-here

# Agent name (displayed in BackupX UI)
AGENT_NAME=production-server

# Logging level
LOG_LEVEL=INFO

# Restrict backup paths (optional, comma-separated)
# Leave empty to allow all mounted paths
ALLOWED_PATHS=/var/www,/home,/etc,/opt
```

### Agent Docker Compose

```yaml
version: "3.8"

services:
  backupx-agent:
    build: .
    container_name: backupx-agent
    restart: unless-stopped
    ports:
      - "8090:8090"
    volumes:
      # Mount paths you want to backup
      - /var/www:/data/www:ro
      - /home:/data/home:ro
      - /etc:/data/etc:ro
    environment:
      - AGENT_API_KEY=${AGENT_API_KEY}
      - AGENT_NAME=${AGENT_NAME:-server-agent}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - ALLOWED_PATHS=${ALLOWED_PATHS:-}
```

### Adding Agent Server in BackupX UI

1. Go to **Configuration > Servers**
2. Click **Add Server**
3. Select **Connection Type: BackupX Agent**
4. Enter:
   - **Name**: Descriptive server name
   - **Host**: Remote server IP or hostname
   - **Agent Port**: 8090 (default)
   - **Agent API Key**: The key from agent's `.env` file
5. Click **Test Connection** to verify
6. Click **Save**

### Agent Security

1. **Firewall Rules**:
   ```bash
   # Only allow BackupX server to connect
   sudo ufw allow from BACKUPX_SERVER_IP to any port 8090
   ```

2. **Use Strong API Keys**:
   ```bash
   openssl rand -hex 32
   ```

3. **Restrict Paths**: Configure `ALLOWED_PATHS` to limit what directories can be backed up

4. **Network Isolation**: Consider placing agent on internal network only accessible by BackupX server

### Agent Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check (no auth) |
| `/info` | GET | Agent information |
| `/backup/filesystem` | POST | Run filesystem backup |
| `/backup/database` | POST | Run MySQL backup |
| `/snapshots` | POST | List snapshots |
| `/stats` | POST | Repository stats |
| `/init` | POST | Initialize repository |

## Remote Server Requirements (SSH Method)

### For Filesystem Backups

Install restic on each remote server:

```bash
# Debian/Ubuntu
sudo apt install restic

# RHEL/CentOS
sudo yum install restic

# Or download binary
wget https://github.com/restic/restic/releases/download/v0.16.0/restic_0.16.0_linux_amd64.bz2
bunzip2 restic_0.16.0_linux_amd64.bz2
chmod +x restic_0.16.0_linux_amd64
sudo mv restic_0.16.0_linux_amd64 /usr/local/bin/restic
```

### For Database Backups

Install MySQL client:

```bash
# Debian/Ubuntu
sudo apt install mysql-client

# RHEL/CentOS
sudo yum install mysql
```

## Backup & Restore

### Backup BackupX Data

```bash
# Stop the container
docker compose stop

# Backup the data volume
docker run --rm -v backupx_backup-data:/data -v $(pwd):/backup alpine \
    tar czf /backup/backupx-data-$(date +%Y%m%d).tar.gz /data

# Backup config and logs
tar czf backupx-config-$(date +%Y%m%d).tar.gz config/ logs/ .env

# Start the container
docker compose start
```

### Restore BackupX Data

```bash
# Stop the container
docker compose stop

# Restore the data volume
docker run --rm -v backupx_backup-data:/data -v $(pwd):/backup alpine \
    tar xzf /backup/backupx-data-YYYYMMDD.tar.gz -C /

# Restore config
tar xzf backupx-config-YYYYMMDD.tar.gz

# Start the container
docker compose up -d
```

## Monitoring

### Health Check

```bash
curl http://localhost:8088/health
```

### View Logs

```bash
# Application logs
docker compose logs -f backup-ui

# Or check the logs directory
tail -f logs/*.log
```

### Container Status

```bash
docker compose ps
docker stats backup-ui
```

## Troubleshooting

### SSH Connection Issues

```bash
# Test SSH from container
docker exec -it backup-ui ssh -i /home/backupx/.ssh/id_rsa user@remote-host

# Check SSH key permissions
ls -la ~/.ssh/
# Keys should be 600, directory should be 700
```

### S3 Connection Issues

```bash
# Test S3 access from container
docker exec -it backup-ui restic -r s3:https://endpoint/bucket snapshots
```

### Database Connection Issues

```bash
# Test MySQL connection from remote server
mysql -h db-host -u user -p -e "SELECT 1"
```

### Container Won't Start

```bash
# Check logs
docker compose logs backup-ui

# Rebuild without cache
docker compose build --no-cache
docker compose up -d
```

## Updating

```bash
# Pull latest changes
git pull origin main

# Rebuild frontend (if needed)
cd frontend && npm install && npm run build && cd ..

# Rebuild and restart container
docker compose up -d --build
```

## Security Recommendations

1. **Use strong passwords** for admin account and all credentials
2. **Enable HTTPS** via reverse proxy
3. **Restrict network access** to the BackupX port
4. **Rotate credentials** periodically
5. **Monitor logs** for unauthorized access attempts
6. **Keep software updated** - Docker, host OS, and BackupX
7. **Use dedicated SSH keys** for backup operations
8. **Limit SSH user permissions** on remote servers to only what's needed for backups

## Support

- GitHub Issues: https://github.com/SaiphMuhammad/backupx/issues
- Documentation: https://github.com/SaiphMuhammad/backupx

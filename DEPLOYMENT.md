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

## Remote Server Requirements

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

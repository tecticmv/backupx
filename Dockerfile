# Stage 1: Build frontend
FROM node:20-slim AS frontend-builder

WORKDIR /app/frontend

# Copy frontend package files
COPY frontend/package*.json ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY frontend/ ./

# Build frontend
RUN npm run build

# Stage 2: Python application
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    openssh-client \
    sshpass \
    bzip2 \
    rclone \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Pinned static restic for the commands BackupX runs itself (snapshot listing,
# restores, retention forget/prune). Debian's package is 0.14, which lacks
# --retry-lock (0.16+) and exit code 10 for a missing repository (0.17+).
ARG RESTIC_VERSION=0.18.1
ARG RESTIC_SHA256_AMD64=680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d
ARG RESTIC_SHA256_ARM64=87f53fddde38764095e9c058a3b31834052c37e5826d2acf34e18923c006bd45
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sha="$RESTIC_SHA256_AMD64" ;; \
        arm64) sha="$RESTIC_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/restic.bz2 \
        "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${arch}.bz2"; \
    echo "$sha  /tmp/restic.bz2" | sha256sum -c -; \
    bunzip2 /tmp/restic.bz2; \
    install -m 0755 /tmp/restic /usr/local/bin/restic; \
    rm -f /tmp/restic; \
    restic version

# Restore wizard (P2): the docker CLI drives scratch restore containers on the
# host daemon (mounted /var/run/docker.sock; the backupx user gets the socket's
# group via DOCKER_GID in docker-compose.yml), and wal-g restores PostgreSQL
# PITR base backups. wal-g v2.0.1 (ubuntu-18.04 build) also runs inside the
# postgres:<major>-bullseye scratch images, where it is copied as restore_command.
ARG DOCKER_CLI_VERSION=27.5.1
ARG DOCKER_CLI_SHA256_AMD64=4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640
ARG DOCKER_CLI_SHA256_ARM64=e6b53725a73763ab3f988c73f8772eaed429754c1a579db5ff11f21990fd1817
ARG WALG_VERSION=v2.0.1
ARG WALG_SHA256_AMD64=21ee4ad9e1e874d1adf7f7fd3b1a79c3d1c6a754bc7d33c7478118fcdf1950d8
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
    amd64) darch=x86_64; dsha="$DOCKER_CLI_SHA256_AMD64" ;; \
    arm64) darch=aarch64; dsha="$DOCKER_CLI_SHA256_ARM64" ;; \
    esac; \
    curl -fsSL -o /tmp/docker.tgz "https://download.docker.com/linux/static/stable/${darch}/docker-${DOCKER_CLI_VERSION}.tgz"; \
    echo "$dsha  /tmp/docker.tgz" | sha256sum -c -; \
    tar -xzf /tmp/docker.tgz -C /tmp docker/docker; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz; \
    if [ "$arch" = amd64 ]; then \
    curl -fsSL -o /tmp/walg.tgz "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-ubuntu-18.04-amd64.tar.gz"; \
    echo "$WALG_SHA256_AMD64  /tmp/walg.tgz" | sha256sum -c -; \
    tar -xzf /tmp/walg.tgz -C /tmp; \
    install -m 0755 /tmp/wal-g-pg-ubuntu-18.04-amd64 /usr/local/bin/wal-g; \
    rm -f /tmp/walg.tgz /tmp/wal-g-pg-ubuntu-18.04-amd64; \
    fi; \
    docker --version

# Create non-root user for running the application
RUN groupadd -r backupx && useradd -r -g backupx -d /app -s /sbin/nologin backupx

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/
COPY static/ ./static/

# Copy React frontend build from builder stage
COPY --from=frontend-builder /app/frontend/dist/ ./frontend/dist/

# Create directories with proper ownership
RUN mkdir -p /app/config /app/logs /app/data/restores /home/backupx/.ssh \
    && chown -R backupx:backupx /app /home/backupx

# Switch to non-root user
USER backupx

# Expose port
EXPOSE 9090

# Run the application with gunicorn
CMD sh -c 'gunicorn --bind "0.0.0.0:${LISTEN_PORT:-9090}" --workers 2 --threads 4 --timeout 300 --graceful-timeout 120 --access-logfile - --error-logfile - app.main:app'

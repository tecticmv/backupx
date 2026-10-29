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
RUN mkdir -p /app/config /app/logs /app/data /home/backupx/.ssh \
    && chown -R backupx:backupx /app /home/backupx

# Switch to non-root user
USER backupx

# Expose port
EXPOSE 9090

# Run the application with gunicorn
CMD sh -c 'gunicorn --bind "0.0.0.0:${LISTEN_PORT:-9090}" --workers 2 --threads 4 --timeout 300 --graceful-timeout 120 --access-logfile - --error-logfile - app.main:app'

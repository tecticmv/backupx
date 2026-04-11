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
    restic \
    rclone \
    cron \
    && rm -rf /var/lib/apt/lists/*

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
CMD sh -c 'gunicorn --bind "0.0.0.0:${LISTEN_PORT:-9090}" --workers 2 --threads 4 --access-logfile - --error-logfile - app.main:app'

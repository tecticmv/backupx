FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    openssh-client \
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
COPY templates/ ./templates/
COPY static/ ./static/

# Copy React frontend build
COPY frontend/dist/ ./frontend/dist/

# Create directories with proper ownership
RUN mkdir -p /app/config /app/logs /app/data /home/backupx/.ssh \
    && chown -R backupx:backupx /app /home/backupx

# Switch to non-root user
USER backupx

# Expose port
EXPOSE 5000

# Run the application with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "app.main:app"]

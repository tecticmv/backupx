FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    restic \
    default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r backupx && useradd -r -g backupx -d /app -s /sbin/nologin backupx

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent application
COPY agent.py .

# Set ownership
RUN chown -R backupx:backupx /app

# Switch to non-root user
USER backupx

# Expose port
EXPOSE 8090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

# Run the agent
CMD ["python", "agent.py"]

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

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

# Copy React frontend build
COPY frontend/dist/ ./frontend/dist/

# Create directories
RUN mkdir -p /app/config /app/logs /app/data

# Expose port
EXPOSE 5000

# Run the application
CMD ["python", "app/main.py"]

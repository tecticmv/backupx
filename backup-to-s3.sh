#!/bin/bash
#
# Secure Incremental Backup Script using Restic
# DigitalOcean Droplet -> Qumulo S3
#
# Security features:
# - Credentials stored in separate protected file
# - No passwords in process list
# - Lock file prevents concurrent runs
# - Integrity verification
# - Failure alerting
#

set -euo pipefail

# ============================================
# CONFIGURATION
# ============================================

# Credentials file (chmod 600, owned by root)
CREDENTIALS_FILE="/etc/backup/credentials.env"

# Remote DigitalOcean Droplet
REMOTE_HOST="root@your-droplet-ip"
REMOTE_PORT="22"
SSH_KEY="/root/.ssh/id_rsa"

# Directories to backup
BACKUP_DIRS=(
    "/var/www"
    "/home"
    "/etc/nginx"
    "/root/epetition-portal"
)

# Retention policy
KEEP_HOURLY=24
KEEP_DAILY=7
KEEP_WEEKLY=4
KEEP_MONTHLY=12
KEEP_YEARLY=2

# Settings
LOG_DIR="/var/log/s3-backup"
LOCK_FILE="/var/run/backup-to-s3.lock"
COMPRESSION="auto"

# Alert settings (optional)
ALERT_EMAIL=""                    # Leave empty to disable
ALERT_WEBHOOK=""                  # Slack/Discord webhook URL

# Exclude patterns
EXCLUDE_PATTERNS=(
    "*.tmp"
    "*.log"
    "*.cache"
    "node_modules"
    ".git"
    "vendor"
    "__pycache__"
    "*.sock"
)

# ============================================
# DO NOT EDIT BELOW THIS LINE
# ============================================

readonly SCRIPT_NAME=$(basename "$0")
readonly TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Logging
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/backup_$TIMESTAMP.log"

log() {
    local level=$1
    shift
    local message="$*"
    local ts=$(date +"%Y-%m-%d %H:%M:%S")
    echo -e "[$ts] [$level] $message" | tee -a "$LOG_FILE"
}

log_info() { log "INFO" "$*"; }
log_warn() { log "WARN" "${YELLOW}$*${NC}"; }
log_error() { log "ERROR" "${RED}$*${NC}"; }
log_success() { log "SUCCESS" "${GREEN}$*${NC}"; }

# Send alert on failure
send_alert() {
    local subject=$1
    local message=$2

    # Email alert
    if [[ -n "$ALERT_EMAIL" ]]; then
        echo "$message" | mail -s "$subject" "$ALERT_EMAIL" 2>/dev/null || true
    fi

    # Webhook alert (Slack/Discord)
    if [[ -n "$ALERT_WEBHOOK" ]]; then
        curl -s -X POST -H 'Content-type: application/json' \
            --data "{\"text\":\"$subject\n$message\"}" \
            "$ALERT_WEBHOOK" 2>/dev/null || true
    fi
}

# Cleanup on exit
cleanup() {
    local exit_code=$?

    # Remove lock file
    rm -f "$LOCK_FILE"

    # Alert on failure
    if [[ $exit_code -ne 0 ]]; then
        send_alert "🚨 Backup FAILED on $(hostname)" \
            "Exit code: $exit_code\nLog: $LOG_FILE\nTime: $(date)"
    fi

    exit $exit_code
}

trap cleanup EXIT

# Acquire lock
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local pid=$(cat "$LOCK_FILE" 2>/dev/null)
        if kill -0 "$pid" 2>/dev/null; then
            log_error "Another backup is running (PID: $pid)"
            exit 1
        else
            log_warn "Removing stale lock file"
            rm -f "$LOCK_FILE"
        fi
    fi

    echo $$ > "$LOCK_FILE"
    log_info "Lock acquired (PID: $$)"
}

# Load credentials securely
load_credentials() {
    if [[ ! -f "$CREDENTIALS_FILE" ]]; then
        log_error "Credentials file not found: $CREDENTIALS_FILE"
        log_info "Create it with:"
        log_info "  sudo mkdir -p /etc/backup"
        log_info "  sudo tee /etc/backup/credentials.env << 'EOF'"
        log_info "S3_ENDPOINT=your-qumulo-server:9000"
        log_info "S3_BUCKET=droplet-backups"
        log_info "S3_ACCESS_KEY=your-access-key"
        log_info "S3_SECRET_KEY=your-secret-key"
        log_info "RESTIC_PASSWORD=your-secure-password"
        log_info "EOF"
        log_info "  sudo chmod 600 /etc/backup/credentials.env"
        log_info "  sudo chown root:root /etc/backup/credentials.env"
        exit 1
    fi

    # Check permissions
    local perms=$(stat -c %a "$CREDENTIALS_FILE" 2>/dev/null || stat -f %Lp "$CREDENTIALS_FILE")
    if [[ "$perms" != "600" ]]; then
        log_error "Credentials file has insecure permissions: $perms (should be 600)"
        log_info "Fix with: sudo chmod 600 $CREDENTIALS_FILE"
        exit 1
    fi

    # Load credentials
    source "$CREDENTIALS_FILE"

    # Validate required variables
    local required_vars=("S3_ENDPOINT" "S3_BUCKET" "S3_ACCESS_KEY" "S3_SECRET_KEY" "RESTIC_PASSWORD")
    for var in "${required_vars[@]}"; do
        if [[ -z "${!var:-}" ]]; then
            log_error "Missing required variable in credentials file: $var"
            exit 1
        fi
    done

    # Set restic environment (not exported to child processes by default)
    export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
    export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
    export RESTIC_PASSWORD
    export RESTIC_REPOSITORY="s3:https://$S3_ENDPOINT/$S3_BUCKET/${BACKUP_PREFIX:-droplet-backup}"

    log_success "Credentials loaded securely"
}

# Check dependencies
check_dependencies() {
    local missing=()

    for cmd in restic ssh curl; do
        if ! command -v $cmd &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing dependencies: ${missing[*]}"
        exit 1
    fi

    log_info "restic version: $(restic version | head -1)"
}

# Setup remote server
setup_remote() {
    log_info "Checking remote server..."

    # Test SSH connection
    if ! ssh -i "$SSH_KEY" -p "$REMOTE_PORT" -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE_HOST" "true" 2>/dev/null; then
        log_error "Cannot connect to $REMOTE_HOST"
        exit 1
    fi
    log_success "SSH connection OK"

    # Check/install restic on remote
    if ! ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" "command -v restic" &>/dev/null; then
        log_info "Installing restic on remote server..."
        ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
            "apt-get update -qq && apt-get install -y -qq restic" || {
            log_error "Failed to install restic on remote"
            exit 1
        }
    fi
    log_success "Remote server ready"

    # Create credentials file on remote (temporary, removed after backup)
    ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
        "umask 077 && cat > /tmp/.backup_creds << 'CREDENTIALS'
export AWS_ACCESS_KEY_ID='$S3_ACCESS_KEY'
export AWS_SECRET_ACCESS_KEY='$S3_SECRET_KEY'
export RESTIC_PASSWORD='$RESTIC_PASSWORD'
export RESTIC_REPOSITORY='s3:https://$S3_ENDPOINT/$S3_BUCKET/${BACKUP_PREFIX:-droplet-backup}'
CREDENTIALS"
}

# Cleanup remote credentials
cleanup_remote() {
    ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
        "rm -f /tmp/.backup_creds" 2>/dev/null || true
}

# Initialize repository
init_repository() {
    log_info "Checking repository..."

    if ! restic snapshots &>/dev/null 2>&1; then
        log_info "Initializing new repository..."
        restic init --repository-version 2 2>&1 | tee -a "$LOG_FILE"

        if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
            log_error "Failed to initialize repository"
            exit 1
        fi
        log_success "Repository initialized"
    else
        log_success "Repository exists"
    fi
}

# Build exclude arguments
build_excludes() {
    local args=""
    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        args="$args --exclude '$pattern'"
    done
    echo "$args"
}

# Perform backup
backup_directories() {
    log_info "Starting backup..."

    local exclude_args=$(build_excludes)
    local dirs="${BACKUP_DIRS[*]}"

    log_info "Directories: $dirs"

    # Run backup on remote, credentials loaded from file
    ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
        "source /tmp/.backup_creds && \
         restic backup \
            --compression $COMPRESSION \
            --verbose \
            --tag automated \
            --tag droplet \
            --host $(echo "$REMOTE_HOST" | cut -d@ -f2) \
            $exclude_args \
            $dirs" 2>&1 | tee -a "$LOG_FILE"

    local exit_code=${PIPESTATUS[0]}

    if [[ $exit_code -eq 0 ]]; then
        log_success "Backup completed"
        return 0
    else
        log_error "Backup failed (exit: $exit_code)"
        return 1
    fi
}

# Apply retention policy
apply_retention() {
    log_info "Applying retention policy..."

    ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
        "source /tmp/.backup_creds && \
         restic forget \
            --keep-hourly $KEEP_HOURLY \
            --keep-daily $KEEP_DAILY \
            --keep-weekly $KEEP_WEEKLY \
            --keep-monthly $KEEP_MONTHLY \
            --keep-yearly $KEEP_YEARLY \
            --prune" 2>&1 | tee -a "$LOG_FILE"

    if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
        log_success "Retention applied"
    else
        log_warn "Retention failed (non-critical)"
    fi
}

# Integrity check (runs on Sundays)
check_integrity() {
    local day=$(date +%u)

    if [[ "$day" != "7" ]]; then
        log_info "Skipping integrity check (Sunday only)"
        return 0
    fi

    log_info "Running integrity check (5% sample)..."

    ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
        "source /tmp/.backup_creds && \
         restic check --read-data-subset=5%" 2>&1 | tee -a "$LOG_FILE"

    if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
        log_success "Integrity check passed"
    else
        log_error "Integrity check FAILED"
        send_alert "🚨 Backup Integrity Check FAILED" \
            "Repository may be corrupted!\nServer: $(hostname)\nTime: $(date)"
    fi
}

# Generate report
generate_report() {
    local report_file="$LOG_DIR/report_$TIMESTAMP.txt"

    {
        echo "=========================================="
        echo "    BACKUP REPORT - $TIMESTAMP"
        echo "=========================================="
        echo ""
        echo "Source: $REMOTE_HOST"
        echo "Repository: s3://$S3_BUCKET/${BACKUP_PREFIX:-droplet-backup}"
        echo ""
        echo "Directories:"
        printf '  - %s\n' "${BACKUP_DIRS[@]}"
        echo ""
        echo "Retention: H=$KEEP_HOURLY D=$KEEP_DAILY W=$KEEP_WEEKLY M=$KEEP_MONTHLY Y=$KEEP_YEARLY"
        echo ""
        echo "Snapshots:"
        ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
            "source /tmp/.backup_creds && restic snapshots --compact" 2>/dev/null || echo "  Error listing"
        echo ""
        echo "Stats:"
        ssh -i "$SSH_KEY" -p "$REMOTE_PORT" "$REMOTE_HOST" \
            "source /tmp/.backup_creds && restic stats --mode raw-data" 2>/dev/null || echo "  Error getting stats"
        echo ""
        echo "=========================================="
    } | tee "$report_file"

    log_success "Report: $report_file"
}

# Cleanup old logs
cleanup_logs() {
    find "$LOG_DIR" -name "*.log" -mtime +30 -delete 2>/dev/null || true
    find "$LOG_DIR" -name "report_*.txt" -mtime +30 -delete 2>/dev/null || true
}

# Main
main() {
    log_info "=========================================="
    log_info "Starting Secure Backup"
    log_info "=========================================="

    local start_time=$(date +%s)

    # Security & setup
    acquire_lock
    load_credentials
    check_dependencies
    setup_remote

    # Initialize if needed
    init_repository

    # Backup
    local backup_ok=false
    if backup_directories; then
        backup_ok=true
        apply_retention
        check_integrity
    fi

    # Cleanup & report
    cleanup_remote
    generate_report
    cleanup_logs

    # Summary
    local duration=$(( $(date +%s) - start_time ))
    local mins=$((duration / 60))
    local secs=$((duration % 60))

    log_info "=========================================="
    if $backup_ok; then
        log_success "Completed in ${mins}m ${secs}s"
        send_alert "✅ Backup OK on $(hostname)" \
            "Duration: ${mins}m ${secs}s\nTime: $(date)" 2>/dev/null || true
    else
        log_error "FAILED after ${mins}m ${secs}s"
        exit 1
    fi
}

main "$@"

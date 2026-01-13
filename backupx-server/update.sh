#!/bin/bash
#
# BackupX Server - Update Script
# This script updates an existing BackupX server installation
#
# Usage: ./update.sh [--no-restart]
#
# Options:
#   --no-restart    Don't restart the server after update
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Parse arguments
NO_RESTART=0
for arg in "$@"; do
    case $arg in
        --no-restart)
            NO_RESTART=1
            shift
            ;;
    esac
done

echo ""
echo "=========================================="
echo "  BackupX Server - Update Script"
echo "=========================================="
echo ""

# =============================================================================
# Pre-update Checks
# =============================================================================

log_step "Checking current installation..."

if [ ! -d "venv" ]; then
    log_error "Virtual environment not found. Please run install.sh first."
    exit 1
fi

if [ ! -f ".env" ]; then
    log_error ".env file not found. Please run install.sh first."
    exit 1
fi

# Check if server is running
SERVER_PID=""
if [ -f "../.pids/server.pid" ]; then
    SERVER_PID=$(cat "../.pids/server.pid" 2>/dev/null || echo "")
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        log_info "Server is running (PID: $SERVER_PID)"
    else
        SERVER_PID=""
    fi
fi

# =============================================================================
# Backup Current State
# =============================================================================

log_step "Creating backup..."

BACKUP_DIR="backups/update-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Backup .env (important!)
cp .env "$BACKUP_DIR/.env.backup"
log_info "Backed up .env to $BACKUP_DIR/"

# =============================================================================
# Pull Latest Code
# =============================================================================

log_step "Pulling latest code from git..."

# Check if we're in a git repo
if [ -d "../.git" ]; then
    cd ..

    # Check for uncommitted changes
    if ! git diff-index --quiet HEAD -- 2>/dev/null; then
        log_warn "You have uncommitted changes. Stashing..."
        git stash
        STASHED=1
    fi

    # Pull latest
    git pull origin main

    # Restore stashed changes if any
    if [ "${STASHED:-0}" = "1" ]; then
        log_info "Restoring stashed changes..."
        git stash pop || log_warn "Could not restore stashed changes. Check 'git stash list'"
    fi

    cd backupx-server
    log_info "Code updated from git"
else
    log_warn "Not a git repository. Skipping code update."
    log_warn "Please manually update the code if needed."
fi

# =============================================================================
# Update Python Dependencies
# =============================================================================

log_step "Updating Python dependencies..."

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt --upgrade
log_info "Python dependencies updated"

# =============================================================================
# Rebuild Frontend
# =============================================================================

log_step "Rebuilding frontend..."

cd frontend
npm install
npm run build
cd ..
log_info "Frontend rebuilt successfully"

# =============================================================================
# Database Migrations
# =============================================================================

log_step "Running database migrations..."

# The app runs migrations automatically on startup
# But we can trigger a check here
python3 -c "
from app.db import init_database
try:
    init_database()
    print('Database migrations completed')
except Exception as e:
    print(f'Migration check: {e}')
" 2>/dev/null || log_warn "Could not verify database migrations (will run on startup)"

# =============================================================================
# Restart Server
# =============================================================================

if [ "$NO_RESTART" = "0" ] && [ -n "$SERVER_PID" ]; then
    log_step "Restarting server..."

    # Use run.sh if available
    if [ -f "../run.sh" ]; then
        cd ..
        ./run.sh server:restart
        cd backupx-server
    else
        # Manual restart
        kill "$SERVER_PID" 2>/dev/null || true
        sleep 2

        source venv/bin/activate
        gunicorn --bind 0.0.0.0:5000 --workers 4 --daemon app.main:app
        log_info "Server restarted"
    fi
elif [ "$NO_RESTART" = "1" ]; then
    log_info "Skipping server restart (--no-restart flag)"
else
    log_info "Server was not running. Start it manually when ready."
fi

# =============================================================================
# Summary
# =============================================================================

echo ""
echo "=========================================="
echo "  Update Complete!"
echo "=========================================="
echo ""
log_info "What was updated:"
echo "  - Python dependencies"
echo "  - Frontend build"
echo "  - Database migrations (if any)"
echo ""

if [ "$NO_RESTART" = "1" ]; then
    log_warn "Server was NOT restarted. Please restart manually:"
    echo "  ./run.sh server:restart"
fi

echo ""
log_info "Backup saved to: $BACKUP_DIR/"
echo ""

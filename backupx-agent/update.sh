#!/bin/bash
#
# BackupX Agent - Update Script
# This script updates an existing BackupX agent installation
#
# Usage: ./update.sh [--no-restart]
#
# Options:
#   --no-restart    Don't restart the agent after update
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
echo "  BackupX Agent - Update Script"
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

# Check if agent is running
AGENT_PID=""

# Check systemd first
if systemctl is-active --quiet backupx-agent 2>/dev/null; then
    log_info "Agent is running via systemd"
    RUNNING_VIA_SYSTEMD=1
# Check pid file
elif [ -f "../.pids/agent.pid" ]; then
    AGENT_PID=$(cat "../.pids/agent.pid" 2>/dev/null || echo "")
    if [ -n "$AGENT_PID" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
        log_info "Agent is running (PID: $AGENT_PID)"
    else
        AGENT_PID=""
    fi
fi

# =============================================================================
# Backup Current State
# =============================================================================

log_step "Creating backup..."

BACKUP_DIR="backups/update-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Backup .env (important - contains API key!)
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

    cd backupx-agent
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
# Update Restic (if possible)
# =============================================================================

log_step "Checking for restic updates..."

if command -v restic &> /dev/null; then
    # Try to self-update restic
    if restic self-update 2>/dev/null; then
        log_info "Restic updated to latest version"
    else
        log_info "Restic self-update not available. Current version: $(restic version | head -n1)"
    fi
else
    log_warn "Restic not found in PATH"
fi

# =============================================================================
# Restart Agent
# =============================================================================

if [ "$NO_RESTART" = "0" ]; then
    if [ "${RUNNING_VIA_SYSTEMD:-0}" = "1" ]; then
        log_step "Restarting agent via systemd..."
        sudo systemctl restart backupx-agent
        log_info "Agent restarted via systemd"
    elif [ -n "$AGENT_PID" ]; then
        log_step "Restarting agent..."

        # Use run.sh if available
        if [ -f "../run.sh" ]; then
            cd ..
            ./run.sh agent:restart
            cd backupx-agent
        else
            # Manual restart
            kill "$AGENT_PID" 2>/dev/null || true
            sleep 2

            source venv/bin/activate
            nohup python agent.py > logs/agent.log 2>&1 &
            log_info "Agent restarted (PID: $!)"
        fi
    else
        log_info "Agent was not running. Start it manually when ready."
    fi
elif [ "$NO_RESTART" = "1" ]; then
    log_info "Skipping agent restart (--no-restart flag)"
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
echo "  - Restic (if self-update available)"
echo ""

if [ "$NO_RESTART" = "1" ]; then
    log_warn "Agent was NOT restarted. Please restart manually:"
    echo "  ./run.sh agent:restart"
    echo "  # or"
    echo "  sudo systemctl restart backupx-agent"
fi

echo ""
log_info "Backup saved to: $BACKUP_DIR/"
echo ""

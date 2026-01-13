#!/bin/bash
#
# BackupX Server - Installation Script
# This script performs a fresh installation of the BackupX server
#
# Usage: ./install.sh
#
# Prerequisites:
#   - Python 3.9+
#   - Node.js 18+
#   - PostgreSQL database (running and accessible)
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

echo ""
echo "=========================================="
echo "  BackupX Server - Installation Script"
echo "=========================================="
echo ""

# =============================================================================
# Prerequisites Check
# =============================================================================

log_step "Checking prerequisites..."

# Check Python
if ! command -v python3 &> /dev/null; then
    log_error "Python 3 is not installed. Please install Python 3.9 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]); then
    log_error "Python 3.9+ is required. Found: Python $PYTHON_VERSION"
    exit 1
fi
log_info "Python $PYTHON_VERSION found"

# Check Node.js
if ! command -v node &> /dev/null; then
    log_error "Node.js is not installed. Please install Node.js 18 or higher."
    exit 1
fi

NODE_VERSION=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    log_error "Node.js 18+ is required. Found: Node.js $NODE_VERSION"
    exit 1
fi
log_info "Node.js $(node -v) found"

# Check npm
if ! command -v npm &> /dev/null; then
    log_error "npm is not installed."
    exit 1
fi
log_info "npm $(npm -v) found"

echo ""

# =============================================================================
# Create Virtual Environment
# =============================================================================

log_step "Creating Python virtual environment..."

if [ -d "venv" ]; then
    log_warn "Virtual environment already exists. Removing..."
    rm -rf venv
fi

python3 -m venv venv
source venv/bin/activate
log_info "Virtual environment created and activated"

# =============================================================================
# Install Python Dependencies
# =============================================================================

log_step "Installing Python dependencies..."

pip install --upgrade pip
pip install -r requirements.txt
log_info "Python dependencies installed"

# =============================================================================
# Configure Environment
# =============================================================================

log_step "Configuring environment..."

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        log_info "Created .env from .env.example"
        log_warn "Please edit .env and configure your settings before starting the server"
    else
        log_error ".env.example not found"
        exit 1
    fi
else
    log_info ".env already exists"
fi

# =============================================================================
# Build Frontend
# =============================================================================

log_step "Building frontend..."

cd frontend

if [ -d "node_modules" ]; then
    log_info "Removing existing node_modules..."
    rm -rf node_modules
fi

npm install
npm run build

cd ..
log_info "Frontend built successfully"

# =============================================================================
# Create Required Directories
# =============================================================================

log_step "Creating required directories..."

mkdir -p data
mkdir -p data/sessions
mkdir -p logs

log_info "Directories created"

# =============================================================================
# Summary
# =============================================================================

echo ""
echo "=========================================="
echo "  Installation Complete!"
echo "=========================================="
echo ""
log_info "Next steps:"
echo ""
echo "  1. Edit .env and configure your settings:"
echo "     - Set SECRET_KEY (generate with: python -c \"import secrets; print(secrets.token_hex(32))\")"
echo "     - Set ADMIN_USERNAME and ADMIN_PASSWORD"
echo "     - Configure DATABASE_HOST, DATABASE_USER, DATABASE_PASSWORD"
echo ""
echo "  2. Ensure PostgreSQL is running and the database exists"
echo ""
echo "  3. Start the server:"
echo "     Development:  source venv/bin/activate && flask --app app.main run --host 0.0.0.0 --port 5000"
echo "     Production:   source venv/bin/activate && gunicorn --bind 0.0.0.0:5000 --workers 4 app.main:app"
echo ""
echo "  Or use the run.sh script from the project root:"
echo "     ./run.sh server:dev    # Development mode"
echo "     ./run.sh server:start  # Production mode"
echo ""

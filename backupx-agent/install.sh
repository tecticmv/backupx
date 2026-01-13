#!/bin/bash
#
# BackupX Agent - Installation Script
# This script performs a fresh installation of the BackupX agent
#
# Usage: ./install.sh
#
# Prerequisites:
#   - Python 3.9+
#   - Restic (will be installed if not present)
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
echo "  BackupX Agent - Installation Script"
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

# Check/Install Restic
if ! command -v restic &> /dev/null; then
    log_warn "Restic is not installed."

    # Detect OS and install restic
    if [ -f /etc/debian_version ]; then
        log_info "Installing restic via apt..."
        sudo apt-get update && sudo apt-get install -y restic
    elif [ -f /etc/redhat-release ]; then
        log_info "Installing restic via yum/dnf..."
        if command -v dnf &> /dev/null; then
            sudo dnf install -y restic
        else
            sudo yum install -y restic
        fi
    elif [ "$(uname)" = "Darwin" ]; then
        if command -v brew &> /dev/null; then
            log_info "Installing restic via Homebrew..."
            brew install restic
        else
            log_error "Please install Homebrew first, then run: brew install restic"
            exit 1
        fi
    else
        log_error "Please install restic manually: https://restic.readthedocs.io/en/latest/020_installation.html"
        exit 1
    fi
fi

RESTIC_VERSION=$(restic version | head -n1)
log_info "Restic found: $RESTIC_VERSION"

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

        # Generate a random API key
        API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        sed -i.bak "s/AGENT_API_KEY=.*/AGENT_API_KEY=$API_KEY/" .env 2>/dev/null || \
            sed -i '' "s/AGENT_API_KEY=.*/AGENT_API_KEY=$API_KEY/" .env

        log_info "Created .env from .env.example"
        log_info "Generated API key: $API_KEY"
        echo ""
        log_warn "IMPORTANT: Save this API key! You'll need it to connect from the BackupX server."
    else
        log_error ".env.example not found"
        exit 1
    fi
else
    log_info ".env already exists"
fi

# =============================================================================
# Create Required Directories
# =============================================================================

log_step "Creating required directories..."

mkdir -p logs
mkdir -p temp

log_info "Directories created"

# =============================================================================
# Create Systemd Service (Optional)
# =============================================================================

if [ -d "/etc/systemd/system" ] && [ "$(id -u)" = "0" ]; then
    log_step "Creating systemd service..."

    cat > /etc/systemd/system/backupx-agent.service << EOF
[Unit]
Description=BackupX Agent
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python agent.py
Restart=always
RestartSec=10
Environment=PATH=$SCRIPT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log_info "Systemd service created: backupx-agent.service"
    log_info "Enable with: sudo systemctl enable backupx-agent"
    log_info "Start with:  sudo systemctl start backupx-agent"
else
    log_info "Skipping systemd service (not running as root or not a systemd system)"
fi

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
echo "  1. Review the .env file and adjust settings if needed:"
echo "     - AGENT_API_KEY is already set (save it for the server configuration)"
echo "     - AGENT_PORT defaults to 8090"
echo "     - ALLOWED_PATHS can restrict which directories can be backed up"
echo ""
echo "  2. Start the agent:"
echo "     source venv/bin/activate && python agent.py"
echo ""
echo "  Or use the run.sh script from the project root:"
echo "     ./run.sh agent:dev    # Development mode"
echo "     ./run.sh agent:start  # Production mode"
echo ""
echo "  3. On the BackupX server, add this agent:"
echo "     - Go to Servers > Add Server"
echo "     - Select 'Agent' connection type"
echo "     - Enter this server's hostname/IP"
echo "     - Enter the API key shown above"
echo ""

# Show the API key again for convenience
if [ -f ".env" ]; then
    API_KEY=$(grep "^AGENT_API_KEY=" .env | cut -d= -f2)
    if [ -n "$API_KEY" ]; then
        echo "  API Key: $API_KEY"
        echo ""
    fi
fi

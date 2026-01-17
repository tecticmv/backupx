#!/bin/bash
#
# BackupX Agent Installer
# One-liner: curl -sSL https://raw.githubusercontent.com/SaiphMuhammad/backupx/main/install-agent.sh | bash
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="/opt/backupx-agent"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                            ║${NC}"
echo -e "${GREEN}║            BackupX Agent Installer                         ║${NC}"
echo -e "${GREEN}║                                                            ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root: sudo bash install-agent.sh"
    exit 1
fi

# Check/Install Docker
check_docker() {
    if command -v docker &> /dev/null && command -v docker compose &> /dev/null; then
        log_info "Docker is already installed"
        return 0
    fi

    log_info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    log_info "Docker installed successfully"
}

# Generate secure API key
generate_api_key() {
    if command -v python3 &> /dev/null; then
        python3 -c "import secrets; print(secrets.token_hex(32))"
    elif command -v openssl &> /dev/null; then
        openssl rand -hex 32
    else
        head -c 32 /dev/urandom | xxd -p
    fi
}

# Main installation
main() {
    check_docker

    # Create install directory
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"

    # Agent configuration
    echo ""
    log_info "Configuring BackupX Agent..."
    echo ""

    # Agent name
    default_name=$(hostname)
    read -p "Agent name [$default_name]: " agent_name
    agent_name=${agent_name:-$default_name}

    # Agent port
    read -p "Agent port [8090]: " agent_port
    agent_port=${agent_port:-8090}

    # API key
    generated_key=$(generate_api_key)
    echo ""
    echo "Generated API Key: $generated_key"
    read -p "Use this API key? [Y/n]: " use_generated
    if [[ "$use_generated" =~ ^[Nn] ]]; then
        while true; do
            read -p "Enter API key (min 32 chars): " api_key
            if [ ${#api_key} -ge 32 ]; then
                break
            fi
            log_error "API key must be at least 32 characters!"
        done
    else
        api_key=$generated_key
    fi

    # Directories to backup
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo "  Select directories to make available for backup"
    echo "  (These will be mounted read-only in the container)"
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo ""

    # Common directories
    declare -a selected_dirs=()
    declare -a common_dirs=(
        "/var/www"
        "/home"
        "/etc"
        "/var/lib/mysql"
        "/var/lib/postgresql"
        "/opt"
        "/srv"
    )

    echo "Common directories (enter numbers separated by space, or 'c' for custom):"
    echo ""
    for i in "${!common_dirs[@]}"; do
        dir="${common_dirs[$i]}"
        if [ -d "$dir" ]; then
            echo "  $((i+1))) $dir"
        else
            echo -e "  $((i+1))) $dir ${YELLOW}(not found)${NC}"
        fi
    done
    echo ""
    echo "  c) Enter custom directories"
    echo "  a) All existing directories"
    echo ""

    read -p "Selection [1 2 3 or c or a]: " dir_selection

    if [[ "$dir_selection" == "a" ]]; then
        for dir in "${common_dirs[@]}"; do
            if [ -d "$dir" ]; then
                selected_dirs+=("$dir")
            fi
        done
    elif [[ "$dir_selection" == "c" ]]; then
        echo ""
        echo "Enter directories to backup (comma-separated):"
        echo "Example: /var/www,/home/user/data,/opt/app"
        read -p "Directories: " custom_dirs
        IFS=',' read -ra selected_dirs <<< "$custom_dirs"
    else
        for num in $dir_selection; do
            idx=$((num-1))
            if [ $idx -ge 0 ] && [ $idx -lt ${#common_dirs[@]} ]; then
                dir="${common_dirs[$idx]}"
                if [ -d "$dir" ]; then
                    selected_dirs+=("$dir")
                else
                    log_warn "Directory $dir does not exist, skipping"
                fi
            fi
        done
    fi

    # Validate at least one directory selected
    if [ ${#selected_dirs[@]} -eq 0 ]; then
        log_warn "No directories selected. Adding /var/www and /home as defaults."
        selected_dirs=("/var/www" "/home")
    fi

    echo ""
    log_info "Selected directories:"
    for dir in "${selected_dirs[@]}"; do
        echo "  - $dir"
    done

    # Build volume mounts for docker-compose
    volume_mounts=""
    for dir in "${selected_dirs[@]}"; do
        # Trim whitespace
        dir=$(echo "$dir" | xargs)
        if [ -n "$dir" ]; then
            volume_mounts+="      - ${dir}:${dir}:ro\n"
        fi
    done

    # Create .env file
    cat > "$INSTALL_DIR/.env" << EOF
# BackupX Agent Configuration
AGENT_API_KEY=$api_key
AGENT_NAME=$agent_name
AGENT_PORT=$agent_port
LOG_LEVEL=INFO
EOF

    log_info "Created $INSTALL_DIR/.env"

    # Create docker-compose.yml
    cat > "$INSTALL_DIR/docker-compose.yml" << EOF
services:
  backupx-agent:
    image: ghcr.io/saiphmuhammad/backupx-agent:latest
    build:
      context: .
      dockerfile: Dockerfile
    container_name: backupx-agent
    restart: unless-stopped
    ports:
      - "\${AGENT_PORT:-8090}:8090"
    environment:
      - AGENT_API_KEY=\${AGENT_API_KEY}
      - AGENT_NAME=\${AGENT_NAME:-backupx-agent}
      - LOG_LEVEL=\${LOG_LEVEL:-INFO}
    volumes:
$(echo -e "$volume_mounts")    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8090/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
EOF

    log_info "Created $INSTALL_DIR/docker-compose.yml"

    # Create Dockerfile (in case image isn't available)
    cat > "$INSTALL_DIR/Dockerfile" << 'EOF'
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    restic \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir flask gunicorn

# Copy agent script
COPY agent.py .

EXPOSE 8090

CMD ["python", "agent.py"]
EOF

    # Download agent.py from repo
    log_info "Downloading agent..."
    curl -sSL "https://raw.githubusercontent.com/SaiphMuhammad/backupx/main/backupx-agent/agent.py" -o "$INSTALL_DIR/agent.py"

    # Start the agent
    echo ""
    read -p "Start agent now? [Y/n]: " start_now
    if [[ ! "$start_now" =~ ^[Nn] ]]; then
        log_info "Starting BackupX Agent..."
        docker compose up -d --build

        # Wait for startup
        sleep 3

        if docker compose ps | grep -q "running\|Up"; then
            log_info "Agent started successfully!"
        else
            log_error "Agent failed to start. Check logs: docker compose logs"
        fi
    fi

    # Get IP address
    ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}' || curl -s ifconfig.me 2>/dev/null || echo "your-server-ip")

    # Print summary
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  ${GREEN}BackupX Agent Installed Successfully!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  Agent Name:    ${YELLOW}$agent_name${NC}"
    echo -e "  Agent Port:    ${YELLOW}$agent_port${NC}"
    echo -e "  API Key:       ${YELLOW}$api_key${NC}"
    echo ""
    echo -e "  Backup Directories:"
    for dir in "${selected_dirs[@]}"; do
        echo -e "    - $dir"
    done
    echo ""
    echo -e "${BLUE}────────────────────────────────────────────────────────────${NC}"
    echo -e "  Add this server in BackupX UI:"
    echo -e "${BLUE}────────────────────────────────────────────────────────────${NC}"
    echo -e "  Connection Type: ${YELLOW}Agent${NC}"
    echo -e "  Host:            ${YELLOW}$ip_addr${NC}"
    echo -e "  Port:            ${YELLOW}$agent_port${NC}"
    echo -e "  API Key:         ${YELLOW}$api_key${NC}"
    echo ""
    echo -e "${BLUE}────────────────────────────────────────────────────────────${NC}"
    echo -e "  Management Commands:"
    echo -e "${BLUE}────────────────────────────────────────────────────────────${NC}"
    echo -e "  cd $INSTALL_DIR"
    echo -e "  docker compose logs -f     # View logs"
    echo -e "  docker compose restart     # Restart agent"
    echo -e "  docker compose down        # Stop agent"
    echo -e "  docker compose up -d       # Start agent"
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""

    # Save connection info to file
    cat > "$INSTALL_DIR/connection-info.txt" << EOF
BackupX Agent Connection Info
=============================
Host: $ip_addr
Port: $agent_port
API Key: $api_key
Agent Name: $agent_name

Backup Directories:
$(printf '  - %s\n' "${selected_dirs[@]}")
EOF

    log_info "Connection info saved to $INSTALL_DIR/connection-info.txt"
}

main "$@"

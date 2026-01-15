#!/bin/bash
#
# BackupX Runner Script
# Production-ready script for running BackupX server and agent
#
# Usage: ./run.sh <command> [options]
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/backupx-server"
AGENT_DIR="$SCRIPT_DIR/backupx-agent"
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"

# Default configuration
SERVER_PORT="${SERVER_PORT:-5000}"
SERVER_WORKERS="${SERVER_WORKERS:-4}"
SERVER_THREADS="${SERVER_THREADS:-2}"
AGENT_PORT="${AGENT_PORT:-8090}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# Helper Functions
# =============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_debug() {
    if [ "${DEBUG:-0}" = "1" ]; then
        echo -e "${BLUE}[DEBUG]${NC} $1"
    fi
}

print_usage() {
    cat << EOF
BackupX Runner - Production Ready

Usage: ./run.sh <command> [options]

Commands:
  server:start    Start the BackupX server (production mode with gunicorn)
  server:dev      Start the BackupX server (development mode with hot reload)
  server:stop     Stop the BackupX server
  server:restart  Restart the BackupX server
  server:status   Check server status

  agent:start     Start the BackupX agent (production mode)
  agent:dev       Start the BackupX agent (development mode)
  agent:stop      Stop the BackupX agent
  agent:restart   Restart the BackupX agent
  agent:status    Check agent status
  agent:info      Show agent configuration

  test            Run tests for server
  test:cov        Run tests with coverage report
  install         Install dependencies for both server and agent
  check           Run pre-deployment checks
  logs            Tail logs (use: ./run.sh logs [server|agent])
  help            Show this help message

Environment Variables:
  SERVER_PORT     Server port (default: 5000)
  SERVER_WORKERS  Gunicorn workers (default: 4)
  SERVER_THREADS  Gunicorn threads per worker (default: 2)
  AGENT_PORT      Agent port (default: 8090)
  DEBUG           Enable debug output (default: 0)

Examples:
  ./run.sh server:start           # Start server in production mode
  ./run.sh server:dev             # Start server in development mode
  ./run.sh agent:start            # Start agent in production mode
  ./run.sh test                   # Run all tests
  ./run.sh check                  # Run pre-deployment checks
  SERVER_WORKERS=8 ./run.sh server:start  # Start with 8 workers

EOF
}

# =============================================================================
# Prerequisite Checks
# =============================================================================

check_python() {
    if ! command -v python3 &> /dev/null; then
        log_error "python3 is not installed"
        exit 1
    fi

    local version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    log_debug "Python version: $version"

    # Check minimum version (3.9+)
    if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 9) else 1)'; then
        return 0
    else
        log_error "Python 3.9+ is required (found: $version)"
        exit 1
    fi
}

check_venv() {
    local dir=$1
    if [ ! -d "$dir/venv" ]; then
        log_warn "Virtual environment not found. Creating..."
        python3 -m venv "$dir/venv"
        log_info "Virtual environment created at $dir/venv"
    fi
}

ensure_directories() {
    mkdir -p "$PID_DIR"
    mkdir -p "$LOG_DIR"
    mkdir -p "$SERVER_DIR/data"
    mkdir -p "$SERVER_DIR/logs"
    mkdir -p "$SERVER_DIR/config"
}

check_env_file() {
    local dir=$1
    local name=$2

    if [ ! -f "$dir/.env" ]; then
        if [ -f "$dir/.env.example" ]; then
            log_warn ".env file not found. Copying from .env.example"
            cp "$dir/.env.example" "$dir/.env"
            log_error "Please update $dir/.env with your configuration before running in production!"
            return 1
        else
            log_error ".env.example not found in $dir"
            return 1
        fi
    fi
    return 0
}

validate_env() {
    local dir=$1
    local name=$2

    source "$dir/.env"

    local errors=0

    if [ "$name" = "server" ]; then
        # SECRET_KEY validation
        if [ -z "$SECRET_KEY" ] || [ "$SECRET_KEY" = "change-this-to-a-secure-random-string" ]; then
            log_error "CRITICAL: SECRET_KEY is not set or using default value!"
            log_error "  Generate a secure key with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            errors=$((errors + 1))
        elif [ ${#SECRET_KEY} -lt 32 ]; then
            log_error "CRITICAL: SECRET_KEY is too short (${#SECRET_KEY} chars). Minimum 32 characters required."
            errors=$((errors + 1))
        fi

        # ADMIN_PASSWORD validation
        if [ -z "$ADMIN_PASSWORD" ]; then
            log_error "CRITICAL: ADMIN_PASSWORD is not set!"
            errors=$((errors + 1))
        elif [ "$ADMIN_PASSWORD" = "changeme" ] || [ "$ADMIN_PASSWORD" = "admin" ] || [ "$ADMIN_PASSWORD" = "password" ]; then
            log_error "CRITICAL: ADMIN_PASSWORD is using a default/weak value!"
            errors=$((errors + 1))
        elif [ ${#ADMIN_PASSWORD} -lt 12 ]; then
            log_error "CRITICAL: ADMIN_PASSWORD is too short (${#ADMIN_PASSWORD} chars). Minimum 12 characters required."
            errors=$((errors + 1))
        fi

        # ADMIN_USERNAME validation
        if [ -z "$ADMIN_USERNAME" ] || [ "$ADMIN_USERNAME" = "admin" ]; then
            log_warn "Consider changing ADMIN_USERNAME from default 'admin'"
        fi
    fi

    if [ "$name" = "agent" ]; then
        # AGENT_API_KEY validation
        if [ -z "$AGENT_API_KEY" ]; then
            log_error "CRITICAL: AGENT_API_KEY is not set!"
            errors=$((errors + 1))
        elif [ ${#AGENT_API_KEY} -lt 32 ]; then
            log_error "CRITICAL: AGENT_API_KEY is too short (${#AGENT_API_KEY} chars). Minimum 32 characters required."
            errors=$((errors + 1))
        fi
    fi

    if [ $errors -gt 0 ]; then
        log_error ""
        log_error "Production startup blocked due to $errors security issue(s)."
        log_error "Fix the issues above or use development mode: ./run.sh server:dev"
    fi

    return $errors
}

# =============================================================================
# Process Management
# =============================================================================

get_pid_file() {
    local name=$1
    echo "$PID_DIR/${name}.pid"
}

get_pid() {
    local name=$1
    local pid_file=$(get_pid_file "$name")

    if [ -f "$pid_file" ]; then
        cat "$pid_file"
    fi
}

is_running() {
    local name=$1
    local pid=$(get_pid "$name")

    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

wait_for_port() {
    local port=$1
    local timeout=${2:-30}
    local count=0

    while ! nc -z localhost "$port" 2>/dev/null; do
        sleep 1
        count=$((count + 1))
        if [ $count -ge $timeout ]; then
            return 1
        fi
    done
    return 0
}

stop_process() {
    local name=$1
    local pid=$(get_pid "$name")
    local pid_file=$(get_pid_file "$name")

    if [ -z "$pid" ]; then
        log_warn "$name is not running (no PID file)"
        return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        log_warn "$name is not running (stale PID file)"
        rm -f "$pid_file"
        return 0
    fi

    log_info "Stopping $name (PID: $pid)..."

    # Graceful shutdown
    kill -TERM "$pid" 2>/dev/null || true

    # Wait for process to stop
    local count=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        count=$((count + 1))
        if [ $count -ge 10 ]; then
            log_warn "Process not responding, sending SIGKILL..."
            kill -9 "$pid" 2>/dev/null || true
            break
        fi
    done

    rm -f "$pid_file"
    log_info "$name stopped"
}

# =============================================================================
# Server Commands
# =============================================================================

server_start() {
    local mode=${1:-production}

    if is_running "server"; then
        log_error "Server is already running (PID: $(get_pid server))"
        exit 1
    fi

    log_info "Starting BackupX Server ($mode mode)..."

    check_venv "$SERVER_DIR"
    ensure_directories

    if ! check_env_file "$SERVER_DIR" "server"; then
        if [ "$mode" = "production" ]; then
            exit 1
        fi
    fi

    if [ "$mode" = "production" ]; then
        if ! validate_env "$SERVER_DIR" "server"; then
            log_error "Environment validation failed. Fix the issues above before starting in production."
            exit 1
        fi
    fi

    cd "$SERVER_DIR"
    source venv/bin/activate

    # Export environment variables
    set -a
    source .env
    set +a

    if [ "$mode" = "production" ]; then
        # Production mode with gunicorn
        log_info "Starting gunicorn with $SERVER_WORKERS workers, $SERVER_THREADS threads on port $SERVER_PORT"

        gunicorn \
            --bind "0.0.0.0:$SERVER_PORT" \
            --workers "$SERVER_WORKERS" \
            --threads "$SERVER_THREADS" \
            --timeout 120 \
            --keep-alive 5 \
            --max-requests 1000 \
            --max-requests-jitter 50 \
            --access-logfile "$LOG_DIR/server-access.log" \
            --error-logfile "$LOG_DIR/server-error.log" \
            --capture-output \
            --pid "$(get_pid_file server)" \
            --daemon \
            "app.main:app"

        # Wait for server to start
        if wait_for_port "$SERVER_PORT" 30; then
            log_info "Server started successfully on http://localhost:$SERVER_PORT"
            log_info "PID: $(get_pid server)"
            log_info "Logs: $LOG_DIR/server-*.log"
        else
            log_error "Server failed to start. Check logs at $LOG_DIR/server-error.log"
            exit 1
        fi
    else
        # Development mode with Flask
        log_info "Starting Flask development server on port $SERVER_PORT"
        log_warn "Do not use development mode in production!"

        python -m flask --app app.main run \
            --host 0.0.0.0 \
            --port "$SERVER_PORT" \
            --debug
    fi
}

server_stop() {
    stop_process "server"
}

server_restart() {
    server_stop
    sleep 2
    server_start production
}

server_status() {
    if is_running "server"; then
        local pid=$(get_pid "server")
        log_info "Server is running (PID: $pid)"

        # Check if responding
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$SERVER_PORT/health" | grep -q "200"; then
            log_info "Health check: OK"
        else
            log_warn "Health check: FAILED"
        fi
    else
        log_warn "Server is not running"
        return 1
    fi
}

# =============================================================================
# Agent Commands
# =============================================================================

agent_start() {
    local mode=${1:-production}

    if is_running "agent"; then
        log_error "Agent is already running (PID: $(get_pid agent))"
        exit 1
    fi

    log_info "Starting BackupX Agent ($mode mode)..."

    check_venv "$AGENT_DIR"
    ensure_directories

    if ! check_env_file "$AGENT_DIR" "agent"; then
        if [ "$mode" = "production" ]; then
            exit 1
        fi
    fi

    if [ "$mode" = "production" ]; then
        if ! validate_env "$AGENT_DIR" "agent"; then
            log_error "Environment validation failed. Fix the issues above before starting in production."
            exit 1
        fi
    fi

    cd "$AGENT_DIR"
    source venv/bin/activate

    # Export environment variables
    set -a
    source .env
    set +a

    local agent_port="${AGENT_PORT:-8090}"

    if [ "$mode" = "production" ]; then
        # Production mode - run in background
        log_info "Starting agent on port $agent_port"

        nohup python agent.py > "$LOG_DIR/agent.log" 2>&1 &
        echo $! > "$(get_pid_file agent)"

        # Wait for agent to start
        if wait_for_port "$agent_port" 30; then
            log_info "Agent started successfully on http://localhost:$agent_port"
            log_info "PID: $(get_pid agent)"
            log_info "Logs: $LOG_DIR/agent.log"
        else
            log_error "Agent failed to start. Check logs at $LOG_DIR/agent.log"
            exit 1
        fi
    else
        # Development mode
        log_info "Starting agent in development mode on port $agent_port"
        python agent.py
    fi
}

agent_stop() {
    stop_process "agent"
}

agent_restart() {
    agent_stop
    sleep 2
    agent_start production
}

agent_status() {
    local agent_port="${AGENT_PORT:-8090}"

    if is_running "agent"; then
        local pid=$(get_pid "agent")
        log_info "Agent is running (PID: $pid)"

        # Check if responding
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$agent_port/health" | grep -q "200"; then
            log_info "Health check: OK"
        else
            log_warn "Health check: FAILED"
        fi
    else
        log_warn "Agent is not running"
        return 1
    fi
}

agent_info() {
    if [ ! -f "$AGENT_DIR/.env" ]; then
        log_error "Agent configuration not found at $AGENT_DIR/.env"
        log_info "Run './run.sh' to set up the agent"
        return 1
    fi

    source "$AGENT_DIR/.env"

    local agent_name="${AGENT_NAME:-backupx-agent}"
    local agent_port="${AGENT_PORT:-8090}"
    local api_key="${AGENT_API_KEY:-}"
    local allowed_paths="${ALLOWED_PATHS:-all}"

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  Agent Configuration"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  Name:          ${YELLOW}$agent_name${NC}"
    echo -e "  Port:          ${YELLOW}$agent_port${NC}"
    echo -e "  API Key:       ${YELLOW}$api_key${NC}"
    echo -e "  Allowed Paths: ${YELLOW}$allowed_paths${NC}"
    echo -e ""
    echo -e "  Config File:   $AGENT_DIR/.env"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"

    # Check if running
    if is_running "agent"; then
        local pid=$(get_pid "agent")
        echo -e "  Status:        ${GREEN}Running (PID: $pid)${NC}"
    else
        echo -e "  Status:        ${YELLOW}Stopped${NC}"
    fi
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

# =============================================================================
# Test Commands
# =============================================================================

run_tests() {
    log_info "Running tests..."

    check_venv "$SERVER_DIR"

    cd "$SERVER_DIR"
    source venv/bin/activate

    # Set test environment variables
    export SECRET_KEY="test-secret-key-for-testing-only-32chars!"
    export ADMIN_USERNAME="testadmin"
    export ADMIN_PASSWORD="testpassword123"

    # Create temp directories for tests
    mkdir -p /tmp/backupx-test/{data,logs,config}

    pytest "$@"
}

run_tests_with_coverage() {
    log_info "Running tests with coverage..."

    check_venv "$SERVER_DIR"

    cd "$SERVER_DIR"
    source venv/bin/activate

    # Set test environment variables
    export SECRET_KEY="test-secret-key-for-testing-only-32chars!"
    export ADMIN_USERNAME="testadmin"
    export ADMIN_PASSWORD="testpassword123"

    pytest --cov=app --cov-report=html --cov-report=term-missing "$@"

    log_info "Coverage report generated at $SERVER_DIR/htmlcov/index.html"
}

# =============================================================================
# Utility Commands
# =============================================================================

install_deps() {
    log_info "Installing dependencies..."

    # Server
    if [ -d "$SERVER_DIR" ]; then
        log_info "Installing server dependencies..."
        check_venv "$SERVER_DIR"
        source "$SERVER_DIR/venv/bin/activate"
        pip install --upgrade pip
        pip install -r "$SERVER_DIR/requirements.txt"
        deactivate
        log_info "Server dependencies installed"
    fi

    # Agent
    if [ -d "$AGENT_DIR" ]; then
        log_info "Installing agent dependencies..."
        check_venv "$AGENT_DIR"
        source "$AGENT_DIR/venv/bin/activate"
        pip install --upgrade pip
        pip install -r "$AGENT_DIR/requirements.txt"
        deactivate
        log_info "Agent dependencies installed"
    fi

    log_info "All dependencies installed!"
}

run_checks() {
    log_info "Running pre-deployment checks..."

    local errors=0

    # Check Python version
    log_info "Checking Python version..."
    check_python

    # Check server environment
    log_info "Checking server configuration..."
    if [ -f "$SERVER_DIR/.env" ]; then
        if validate_env "$SERVER_DIR" "server"; then
            log_info "Server configuration: OK"
        else
            errors=$((errors + 1))
        fi
    else
        log_error "Server .env file not found"
        errors=$((errors + 1))
    fi

    # Check agent environment
    log_info "Checking agent configuration..."
    if [ -f "$AGENT_DIR/.env" ]; then
        if validate_env "$AGENT_DIR" "agent"; then
            log_info "Agent configuration: OK"
        else
            errors=$((errors + 1))
        fi
    else
        log_warn "Agent .env file not found (optional)"
    fi

    # Check dependencies
    log_info "Checking dependencies..."
    if [ -d "$SERVER_DIR/venv" ]; then
        source "$SERVER_DIR/venv/bin/activate"
        if python -c "import flask; import cryptography; import flask_limiter" 2>/dev/null; then
            log_info "Server dependencies: OK"
        else
            log_error "Server dependencies incomplete. Run: ./run.sh install"
            errors=$((errors + 1))
        fi
        deactivate
    else
        log_error "Server virtual environment not found. Run: ./run.sh install"
        errors=$((errors + 1))
    fi

    # Run tests
    log_info "Running tests..."
    if run_tests -q; then
        log_info "Tests: PASSED"
    else
        log_error "Tests: FAILED"
        errors=$((errors + 1))
    fi

    echo ""
    if [ $errors -eq 0 ]; then
        log_info "All checks passed! Ready for deployment."
    else
        log_error "$errors check(s) failed. Please fix the issues above."
        exit 1
    fi
}

tail_logs() {
    local component=${1:-all}

    ensure_directories

    case "$component" in
        server)
            tail -f "$LOG_DIR/server-access.log" "$LOG_DIR/server-error.log"
            ;;
        agent)
            tail -f "$LOG_DIR/agent.log"
            ;;
        all|*)
            tail -f "$LOG_DIR"/*.log
            ;;
    esac
}

# =============================================================================
# Interactive Setup
# =============================================================================

check_docker() {
    if command -v docker &> /dev/null && command -v docker compose &> /dev/null; then
        return 0
    fi
    return 1
}

choose_runtime() {
    local component=$1

    echo "" >&2
    echo "How do you want to run the $component?" >&2
    echo "" >&2

    if check_docker; then
        echo "  1) Docker (recommended - no dependencies needed)" >&2
        echo "  2) Native Python (requires Python 3.9+, venv, npm)" >&2
        echo "" >&2
        read -p "Enter choice [1/2]: " runtime_choice

        if [ "$runtime_choice" = "1" ]; then
            echo "docker"
        else
            echo "native"
        fi
    else
        log_warn "Docker not found. Using native Python." >&2
        echo "native"
    fi
}

interactive_setup() {
    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                                                            ║${NC}"
    echo -e "${GREEN}║              Welcome to BackupX Setup!                     ║${NC}"
    echo -e "${GREEN}║                                                            ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "What would you like to set up?"
    echo ""
    echo "  1) Server    - Web UI + API (for managing backups)"
    echo "  2) Agent     - Remote backup service (runs on servers to backup)"
    echo "  3) Both      - Install both server and agent"
    echo "  4) Help      - Show all available commands"
    echo "  5) Exit"
    echo ""
    read -p "Enter choice [1-5]: " choice

    case $choice in
        1)
            local runtime=$(choose_runtime "server")
            if [ "$runtime" = "docker" ]; then
                setup_server_docker
            else
                setup_server
            fi
            ;;
        2)
            local runtime=$(choose_runtime "agent")
            if [ "$runtime" = "docker" ]; then
                setup_agent_docker
            else
                setup_agent
            fi
            ;;
        3)
            local runtime=$(choose_runtime "server and agent")
            if [ "$runtime" = "docker" ]; then
                setup_server_docker
                echo ""
                setup_agent_docker
            else
                setup_server
                echo ""
                setup_agent
            fi
            ;;
        4)
            print_usage
            ;;
        5)
            echo "Goodbye!"
            exit 0
            ;;
        *)
            log_error "Invalid choice. Please enter 1-5."
            interactive_setup
            ;;
    esac
}

# =============================================================================
# Docker Setup Functions
# =============================================================================

setup_server_docker() {
    echo ""
    log_info "Setting up BackupX Server with Docker..."
    echo ""

    # Check if .env exists
    if [ ! -f "$SERVER_DIR/.env" ]; then
        log_info "Creating server configuration..."
        cp "$SERVER_DIR/.env.example" "$SERVER_DIR/.env"

        # Generate secure secrets
        local secret_key=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
        local admin_pass=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))" 2>/dev/null || openssl rand -base64 16)

        # Prompt for configuration
        echo ""
        read -p "Enter admin username [admin]: " admin_user
        admin_user=${admin_user:-admin}

        echo ""
        echo "Generated secure admin password: $admin_pass"
        read -p "Use this password? [Y/n]: " use_generated
        if [[ "$use_generated" =~ ^[Nn] ]]; then
            while true; do
                read -s -p "Enter admin password (min 12 chars): " admin_pass
                echo ""
                if [ ${#admin_pass} -ge 12 ]; then
                    break
                fi
                log_error "Password must be at least 12 characters!"
            done
        fi

        echo ""
        read -p "Enter server port [5000]: " server_port
        server_port=${server_port:-5000}

        # Update .env file
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=$secret_key/" "$SERVER_DIR/.env"
            sed -i '' "s/^ADMIN_USERNAME=.*/ADMIN_USERNAME=$admin_user/" "$SERVER_DIR/.env"
            sed -i '' "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$admin_pass/" "$SERVER_DIR/.env"
        else
            sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$secret_key/" "$SERVER_DIR/.env"
            sed -i "s/^ADMIN_USERNAME=.*/ADMIN_USERNAME=$admin_user/" "$SERVER_DIR/.env"
            sed -i "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$admin_pass/" "$SERVER_DIR/.env"
        fi

        log_info "Configuration saved to $SERVER_DIR/.env"
    else
        log_info "Server configuration already exists at $SERVER_DIR/.env"
        source "$SERVER_DIR/.env"
        admin_user="${ADMIN_USERNAME:-admin}"
        admin_pass="(existing - check .env file)"
        server_port="${SERVER_PORT:-5000}"
    fi

    # Build and start with Docker
    echo ""
    read -p "Build and start Docker container? [Y/n]: " start_docker
    if [[ ! "$start_docker" =~ ^[Nn] ]]; then
        log_info "Building Docker image (this may take a few minutes)..."
        cd "$SERVER_DIR"

        # Build frontend if dist doesn't exist
        if [ ! -d "frontend/dist" ] || [ ! -f "frontend/dist/index.html" ]; then
            log_info "Building frontend..."
            if command -v npm &> /dev/null; then
                cd frontend
                npm install --silent 2>/dev/null
                npm run build --silent 2>/dev/null
                cd ..
            else
                log_warn "npm not found. Frontend will be built inside Docker."
            fi
        fi

        docker compose down 2>/dev/null
        docker compose up -d --build

        if [ $? -eq 0 ]; then
            log_info "Docker container started successfully!"
            log_info "Server running at http://localhost:$server_port"
        else
            log_error "Failed to start Docker container. Check docker compose logs."
        fi
        cd - > /dev/null
    fi

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  Server Credentials:"
    echo -e "  Username: ${YELLOW}$admin_user${NC}"
    echo -e "  Password: ${YELLOW}$admin_pass${NC}"
    echo -e ""
    echo -e "  Docker Commands:"
    echo -e "  Start:   cd backupx-server && docker compose up -d"
    echo -e "  Stop:    cd backupx-server && docker compose down"
    echo -e "  Logs:    cd backupx-server && docker compose logs -f"
    echo -e "  Rebuild: cd backupx-server && docker compose up -d --build"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

setup_agent_docker() {
    echo ""
    log_info "Setting up BackupX Agent with Docker..."
    echo ""

    # Check if .env exists
    if [ ! -f "$AGENT_DIR/.env" ]; then
        log_info "Creating agent configuration..."
        cp "$AGENT_DIR/.env.example" "$AGENT_DIR/.env"

        # Generate secure API key
        local api_key=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)

        # Prompt for configuration
        echo ""
        read -p "Enter agent name [$(hostname)]: " agent_name
        agent_name=${agent_name:-$(hostname)}

        echo ""
        read -p "Enter agent port [8090]: " agent_port
        agent_port=${agent_port:-8090}

        echo ""
        echo "Generated API Key: $api_key"
        read -p "Use this API key? [Y/n]: " use_generated
        if [[ "$use_generated" =~ ^[Nn] ]]; then
            while true; do
                read -p "Enter API key (min 32 chars): " api_key
                if [ ${#api_key} -ge 32 ]; then
                    break
                fi
                log_error "API key must be at least 32 characters!"
            done
        fi

        echo ""
        echo "Which paths should be available for backup?"
        echo "(These will be mounted read-only in the Docker container)"
        echo "Example: /var/www,/home,/etc"
        read -p "Paths to backup [/var/www,/home,/etc]: " backup_paths
        backup_paths=${backup_paths:-/var/www,/home,/etc}

        # Update .env file
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s/^AGENT_API_KEY=.*/AGENT_API_KEY=$api_key/" "$AGENT_DIR/.env"
            sed -i '' "s/^AGENT_NAME=.*/AGENT_NAME=$agent_name/" "$AGENT_DIR/.env"
            sed -i '' "s/^AGENT_PORT=.*/AGENT_PORT=$agent_port/" "$AGENT_DIR/.env"
        else
            sed -i "s/^AGENT_API_KEY=.*/AGENT_API_KEY=$api_key/" "$AGENT_DIR/.env"
            sed -i "s/^AGENT_NAME=.*/AGENT_NAME=$agent_name/" "$AGENT_DIR/.env"
            sed -i "s/^AGENT_PORT=.*/AGENT_PORT=$agent_port/" "$AGENT_DIR/.env"
        fi

        # Update docker-compose.yml with backup paths
        log_info "Configuring Docker volumes for backup paths..."

        log_info "Configuration saved to $AGENT_DIR/.env"
    else
        log_info "Agent configuration already exists at $AGENT_DIR/.env"
        source "$AGENT_DIR/.env"
        api_key="$AGENT_API_KEY"
        agent_name="$AGENT_NAME"
        agent_port="${AGENT_PORT:-8090}"
    fi

    # Build and start with Docker
    echo ""
    read -p "Build and start Docker container? [Y/n]: " start_docker
    if [[ ! "$start_docker" =~ ^[Nn] ]]; then
        log_info "Building Docker image..."
        cd "$AGENT_DIR"

        docker compose down 2>/dev/null
        docker compose up -d --build

        if [ $? -eq 0 ]; then
            log_info "Docker container started successfully!"
            log_info "Agent running on port $agent_port"
        else
            log_error "Failed to start Docker container. Check docker compose logs."
        fi
        cd - > /dev/null
    fi

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  Agent Configuration:"
    echo -e "  Name:    ${YELLOW}$agent_name${NC}"
    echo -e "  Port:    ${YELLOW}$agent_port${NC}"
    echo -e "  API Key: ${YELLOW}$api_key${NC}"
    echo -e ""
    echo -e "  Add this server in BackupX UI with:"
    echo -e "  - Connection Type: Agent"
    echo -e "  - Host: $(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-server-ip")"
    echo -e "  - Port: $agent_port"
    echo -e "  - API Key: (shown above)"
    echo -e ""
    echo -e "  Docker Commands:"
    echo -e "  Start:   cd backupx-agent && docker compose up -d"
    echo -e "  Stop:    cd backupx-agent && docker compose down"
    echo -e "  Logs:    cd backupx-agent && docker compose logs -f"
    echo -e "  Rebuild: cd backupx-agent && docker compose up -d --build"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

setup_server() {
    echo ""
    log_info "Setting up BackupX Server..."
    echo ""

    # Check if .env exists
    if [ ! -f "$SERVER_DIR/.env" ]; then
        log_info "Creating server configuration..."
        cp "$SERVER_DIR/.env.example" "$SERVER_DIR/.env"

        # Generate secure secrets
        local secret_key=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        local admin_pass=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")

        # Prompt for configuration
        echo ""
        read -p "Enter admin username [admin]: " admin_user
        admin_user=${admin_user:-admin}

        echo ""
        echo "Generated secure admin password: $admin_pass"
        read -p "Use this password? [Y/n]: " use_generated
        if [[ "$use_generated" =~ ^[Nn] ]]; then
            while true; do
                read -s -p "Enter admin password (min 12 chars): " admin_pass
                echo ""
                if [ ${#admin_pass} -ge 12 ]; then
                    break
                fi
                log_error "Password must be at least 12 characters!"
            done
        fi

        echo ""
        read -p "Enter server port [5000]: " server_port
        server_port=${server_port:-5000}

        # Update .env file
        if [[ "$OSTYPE" == "darwin"* ]]; then
            # macOS
            sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=$secret_key/" "$SERVER_DIR/.env"
            sed -i '' "s/^ADMIN_USERNAME=.*/ADMIN_USERNAME=$admin_user/" "$SERVER_DIR/.env"
            sed -i '' "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$admin_pass/" "$SERVER_DIR/.env"
        else
            # Linux
            sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$secret_key/" "$SERVER_DIR/.env"
            sed -i "s/^ADMIN_USERNAME=.*/ADMIN_USERNAME=$admin_user/" "$SERVER_DIR/.env"
            sed -i "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$admin_pass/" "$SERVER_DIR/.env"
        fi

        log_info "Configuration saved to $SERVER_DIR/.env"
    else
        log_info "Server configuration already exists at $SERVER_DIR/.env"
    fi

    # Install dependencies
    echo ""
    read -p "Install Python dependencies? [Y/n]: " install_deps
    if [[ ! "$install_deps" =~ ^[Nn] ]]; then
        log_info "Installing server dependencies..."
        check_venv "$SERVER_DIR"
        source "$SERVER_DIR/venv/bin/activate"
        pip install --upgrade pip -q
        pip install -r "$SERVER_DIR/requirements.txt" -q
        deactivate
        log_info "Dependencies installed!"
    fi

    # Build frontend
    if [ -d "$SERVER_DIR/frontend" ]; then
        echo ""
        read -p "Build frontend? [Y/n]: " build_fe
        if [[ ! "$build_fe" =~ ^[Nn] ]]; then
            if command -v npm &> /dev/null; then
                log_info "Building frontend..."
                cd "$SERVER_DIR/frontend"
                npm install -q 2>/dev/null
                npm run build -q 2>/dev/null
                cd - > /dev/null
                log_info "Frontend built!"
            else
                log_warn "npm not found. Skipping frontend build."
                log_warn "Install Node.js and run: cd backupx-server/frontend && npm install && npm run build"
            fi
        fi
    fi

    # Start server
    echo ""
    read -p "Start server now? [Y/n]: " start_now
    if [[ ! "$start_now" =~ ^[Nn] ]]; then
        echo ""
        echo "Start in which mode?"
        echo "  1) Production (recommended for real use)"
        echo "  2) Development (hot reload, debug mode)"
        read -p "Enter choice [1/2]: " mode_choice

        if [ "$mode_choice" = "2" ]; then
            server_start development
        else
            server_start production
        fi
    else
        echo ""
        log_info "Server setup complete!"
        echo ""
        echo "To start the server later, run:"
        echo "  ./run.sh server:start     # Production mode"
        echo "  ./run.sh server:dev       # Development mode"
    fi

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  Server Credentials:"
    echo -e "  Username: ${YELLOW}$admin_user${NC}"
    echo -e "  Password: ${YELLOW}$admin_pass${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

setup_agent() {
    echo ""
    log_info "Setting up BackupX Agent..."
    echo ""

    # Check if .env exists
    if [ ! -f "$AGENT_DIR/.env" ]; then
        log_info "Creating agent configuration..."
        cp "$AGENT_DIR/.env.example" "$AGENT_DIR/.env"

        # Generate secure API key
        local api_key=$(python3 -c "import secrets; print(secrets.token_hex(32))")

        # Prompt for configuration
        echo ""
        read -p "Enter agent name [$(hostname)]: " agent_name
        agent_name=${agent_name:-$(hostname)}

        echo ""
        read -p "Enter agent port [8090]: " agent_port
        agent_port=${agent_port:-8090}

        echo ""
        echo "Generated API Key: $api_key"
        read -p "Use this API key? [Y/n]: " use_generated
        if [[ "$use_generated" =~ ^[Nn] ]]; then
            while true; do
                read -p "Enter API key (min 32 chars): " api_key
                if [ ${#api_key} -ge 32 ]; then
                    break
                fi
                log_error "API key must be at least 32 characters!"
            done
        fi

        echo ""
        echo "Restrict backup paths? (comma-separated, empty for all)"
        echo "Example: /var/www,/home,/etc"
        read -p "Allowed paths [all]: " allowed_paths

        # Update .env file
        if [[ "$OSTYPE" == "darwin"* ]]; then
            # macOS
            sed -i '' "s/^AGENT_API_KEY=.*/AGENT_API_KEY=$api_key/" "$AGENT_DIR/.env"
            sed -i '' "s/^AGENT_NAME=.*/AGENT_NAME=$agent_name/" "$AGENT_DIR/.env"
            sed -i '' "s/^AGENT_PORT=.*/AGENT_PORT=$agent_port/" "$AGENT_DIR/.env"
            if [ -n "$allowed_paths" ]; then
                sed -i '' "s/^ALLOWED_PATHS=.*/ALLOWED_PATHS=$allowed_paths/" "$AGENT_DIR/.env"
            fi
        else
            # Linux
            sed -i "s/^AGENT_API_KEY=.*/AGENT_API_KEY=$api_key/" "$AGENT_DIR/.env"
            sed -i "s/^AGENT_NAME=.*/AGENT_NAME=$agent_name/" "$AGENT_DIR/.env"
            sed -i "s/^AGENT_PORT=.*/AGENT_PORT=$agent_port/" "$AGENT_DIR/.env"
            if [ -n "$allowed_paths" ]; then
                sed -i "s/^ALLOWED_PATHS=.*/ALLOWED_PATHS=$allowed_paths/" "$AGENT_DIR/.env"
            fi
        fi

        log_info "Configuration saved to $AGENT_DIR/.env"
    else
        log_info "Agent configuration already exists at $AGENT_DIR/.env"
        # Read existing API key for display
        source "$AGENT_DIR/.env"
        api_key="$AGENT_API_KEY"
        agent_name="$AGENT_NAME"
        agent_port="${AGENT_PORT:-8090}"
    fi

    # Install dependencies
    echo ""
    read -p "Install Python dependencies? [Y/n]: " install_deps
    if [[ ! "$install_deps" =~ ^[Nn] ]]; then
        log_info "Installing agent dependencies..."
        check_venv "$AGENT_DIR"
        source "$AGENT_DIR/venv/bin/activate"
        pip install --upgrade pip -q
        pip install -r "$AGENT_DIR/requirements.txt" -q
        deactivate
        log_info "Dependencies installed!"
    fi

    # Start agent
    echo ""
    read -p "Start agent now? [Y/n]: " start_now
    if [[ ! "$start_now" =~ ^[Nn] ]]; then
        echo ""
        echo "Start in which mode?"
        echo "  1) Production (background daemon)"
        echo "  2) Development (foreground with logs)"
        read -p "Enter choice [1/2]: " mode_choice

        if [ "$mode_choice" = "2" ]; then
            agent_start development
        else
            agent_start production
        fi
    else
        echo ""
        log_info "Agent setup complete!"
        echo ""
        echo "To start the agent later, run:"
        echo "  ./run.sh agent:start     # Production mode"
        echo "  ./run.sh agent:dev       # Development mode"
    fi

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "  Agent Configuration:"
    echo -e "  Name:    ${YELLOW}$agent_name${NC}"
    echo -e "  Port:    ${YELLOW}$agent_port${NC}"
    echo -e "  API Key: ${YELLOW}$api_key${NC}"
    echo -e ""
    echo -e "  Add this server in BackupX UI with:"
    echo -e "  - Connection Type: Agent"
    echo -e "  - Host: $(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-server-ip")"
    echo -e "  - Port: $agent_port"
    echo -e "  - API Key: (shown above)"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

# =============================================================================
# Main
# =============================================================================

check_python

case "${1:-}" in
    "")
        interactive_setup
        ;;
    server:start)
        server_start production
        ;;
    server:dev)
        server_start development
        ;;
    server:stop)
        server_stop
        ;;
    server:restart)
        server_restart
        ;;
    server:status)
        server_status
        ;;
    agent:start)
        agent_start production
        ;;
    agent:dev)
        agent_start development
        ;;
    agent:stop)
        agent_stop
        ;;
    agent:restart)
        agent_restart
        ;;
    agent:status)
        agent_status
        ;;
    agent:info)
        agent_info
        ;;
    test)
        shift
        run_tests "$@"
        ;;
    test:cov)
        shift
        run_tests_with_coverage "$@"
        ;;
    install)
        install_deps
        ;;
    check)
        run_checks
        ;;
    logs)
        tail_logs "${2:-all}"
        ;;
    help|--help|-h)
        print_usage
        ;;
    *)
        log_error "Unknown command: $1"
        echo ""
        print_usage
        exit 1
        ;;
esac
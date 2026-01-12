# BackupX Development Workflow

## Overview

- **Server** (`backupx-server`): Runs on your backup server - manages jobs, web UI, stores config
- **Agent** (`backupx-agent`): Deployed on remote servers you want to back up (optional)

## Development Setup

```bash
# Clone and setup
git clone <repository-url>
cd backupx

# Install all dependencies
./run.sh install

# Copy and configure environment
cp backupx-server/.env.example backupx-server/.env
# Edit the .env file with development values

# Start development server
./run.sh server:dev
```

## Daily Development

### Starting Work

```bash
# Check server status
./run.sh server:status

# Start server in development mode
./run.sh server:dev
```

### Running Tests

```bash
# Run all tests
./run.sh test

# Run tests with coverage
./run.sh test:cov

# Run specific test file
./run.sh test tests/test_security.py

# Run specific test
./run.sh test tests/test_security.py::TestAuthentication::test_login_success
```

### Code Changes

1. Make changes to code
2. Run tests: `./run.sh test`
3. Check for issues: `./run.sh check`
4. Commit changes

## Deployment Workflow

### Pre-Deployment

```bash
# 1. Run all checks
./run.sh check

# This validates:
# - Python version
# - Environment configuration
# - Dependencies
# - Test suite
```

### Deploy Server (Backup Server)

```bash
# 1. Stop existing server
./run.sh server:stop

# 2. Pull latest code
git pull origin main

# 3. Install/update dependencies
./run.sh install

# 4. Run checks
./run.sh check

# 5. Start server
./run.sh server:start

# 6. Verify health
./run.sh server:status
```

### Deploy Agent (Remote Servers)

Copy `backupx-agent/` to each remote server you want to back up:

```bash
# On remote server
cd backupx-agent

# Create venv and install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with AGENT_API_KEY (must match server config)

# Start agent
python agent.py
```

### Monitoring

```bash
# View server logs
./run.sh logs server

# Check health endpoint
curl http://localhost:5000/health
```

## Troubleshooting

### Server Won't Start

```bash
# Check if already running
./run.sh server:status

# Check logs
./run.sh logs server

# Verify environment
./run.sh check
```

### Tests Failing

```bash
# Run with verbose output
./run.sh test -v

# Run specific failing test
./run.sh test tests/test_file.py::test_name -v
```

### Port Already in Use

```bash
# Find process using port
lsof -i :5000

# Kill if necessary
kill -9 <PID>

# Or change port
SERVER_PORT=5001 ./run.sh server:start
```

## Git Workflow

### Feature Development

```bash
# Create feature branch
git checkout -b feature/my-feature

# Make changes and test
./run.sh test

# Commit
git add .
git commit -m "Add my feature"

# Push and create PR
git push -u origin feature/my-feature
```

### Hotfix

```bash
# Create hotfix branch from main
git checkout main
git pull
git checkout -b hotfix/critical-fix

# Make fix
./run.sh test
git commit -am "Fix critical issue"

# Merge to main
git checkout main
git merge hotfix/critical-fix
git push
```

## Environment Management

### Generating Secure Credentials

```bash
# Generate SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# Generate AGENT_API_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# Generate secure password
python3 -c "import secrets; print(secrets.token_urlsafe(16))"
```

### Environment Files

| File | Purpose |
|------|---------|
| `backupx-server/.env` | Server configuration (gitignored) |
| `backupx-server/.env.example` | Server template (committed) |
| `backupx-agent/.env` | Agent configuration (gitignored) |
| `backupx-agent/.env.example` | Agent template (committed) |
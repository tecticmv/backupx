#!/usr/bin/env python3
"""
Backup Manager UI
A web interface for managing restic backups
"""

import os
import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
import yaml
import humanize

# Initialize Flask app
FRONTEND_DIST = Path(__file__).parent.parent / 'frontend' / 'dist'
app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')

# CSRF Protection
csrf = CSRFProtect(app)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Paths
CONFIG_DIR = Path('/app/config')
LOGS_DIR = Path('/app/logs')
DATA_DIR = Path('/app/data')
JOBS_FILE = DATA_DIR / 'jobs.json'
HISTORY_FILE = DATA_DIR / 'history.json'
S3_CONFIGS_FILE = DATA_DIR / 's3_configs.json'
SERVERS_FILE = DATA_DIR / 'servers.json'

# Ensure directories exist
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Scheduler
scheduler = BackgroundScheduler()
scheduler.start()


# User class for authentication
class User(UserMixin):
    def __init__(self, id):
        self.id = id


@login_manager.user_loader
def load_user(user_id):
    if user_id == os.environ.get('ADMIN_USERNAME', 'admin'):
        return User(user_id)
    return None


# Helper functions
def load_jobs():
    """Load backup jobs from file"""
    if JOBS_FILE.exists():
        with open(JOBS_FILE) as f:
            return json.load(f)
    return {}


def save_jobs(jobs):
    """Save backup jobs to file"""
    with open(JOBS_FILE, 'w') as f:
        json.dump(jobs, f, indent=2)


def load_history():
    """Load backup history"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(history):
    """Save backup history"""
    # Keep only last 100 entries
    history = history[-100:]
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)


def add_history(job_id, job_name, status, message, duration=0):
    """Add entry to backup history"""
    history = load_history()
    history.append({
        'timestamp': datetime.now().isoformat(),
        'job_id': job_id,
        'job_name': job_name,
        'status': status,
        'message': message,
        'duration': duration
    })
    save_history(history)


def load_s3_configs():
    """Load S3 configurations from file"""
    if S3_CONFIGS_FILE.exists():
        with open(S3_CONFIGS_FILE) as f:
            return json.load(f)
    return []


def save_s3_configs(configs):
    """Save S3 configurations to file"""
    with open(S3_CONFIGS_FILE, 'w') as f:
        json.dump(configs, f, indent=2)


def load_servers():
    """Load servers from file"""
    if SERVERS_FILE.exists():
        with open(SERVERS_FILE) as f:
            return json.load(f)
    return []


def save_servers(servers):
    """Save servers to file"""
    with open(SERVERS_FILE, 'w') as f:
        json.dump(servers, f, indent=2)


def generate_id():
    """Generate a unique ID"""
    import uuid
    return str(uuid.uuid4())[:8]


def run_backup(job_id):
    """Execute a backup job"""
    jobs = load_jobs()
    if job_id not in jobs:
        return False, "Job not found"

    job = jobs[job_id]
    start_time = datetime.now()

    # Update job status
    jobs[job_id]['status'] = 'running'
    jobs[job_id]['last_run'] = start_time.isoformat()
    save_jobs(jobs)

    try:
        # Build restic command
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        # Build exclude args
        excludes = []
        for pattern in job.get('excludes', []):
            excludes.extend(['--exclude', pattern])

        # Run backup via SSH on remote
        ssh_cmd = [
            'ssh', '-i', job.get('ssh_key', '/root/.ssh/id_rsa'),
            '-p', str(job.get('ssh_port', 22)),
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'BatchMode=yes',
            job['remote_host']
        ]

        # Build remote command
        remote_cmd = f"""
export AWS_ACCESS_KEY_ID='{job['s3_access_key']}'
export AWS_SECRET_ACCESS_KEY='{job['s3_secret_key']}'
export RESTIC_PASSWORD='{job['restic_password']}'
export RESTIC_REPOSITORY='s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}'
restic backup --compression auto --tag automated {' '.join(excludes)} {' '.join(job['directories'])}
"""

        # Execute
        result = subprocess.run(
            ssh_cmd + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=job.get('timeout', 7200)  # 2 hour default timeout
        )

        duration = (datetime.now() - start_time).total_seconds()

        if result.returncode == 0:
            jobs[job_id]['status'] = 'success'
            jobs[job_id]['last_success'] = datetime.now().isoformat()
            save_jobs(jobs)
            add_history(job_id, job['name'], 'success', 'Backup completed successfully', duration)
            return True, result.stdout
        else:
            jobs[job_id]['status'] = 'failed'
            save_jobs(jobs)
            add_history(job_id, job['name'], 'failed', result.stderr, duration)
            return False, result.stderr

    except subprocess.TimeoutExpired:
        jobs[job_id]['status'] = 'timeout'
        save_jobs(jobs)
        add_history(job_id, job['name'], 'timeout', 'Backup timed out', 0)
        return False, "Backup timed out"
    except Exception as e:
        jobs[job_id]['status'] = 'error'
        save_jobs(jobs)
        add_history(job_id, job['name'], 'error', str(e), 0)
        return False, str(e)


def get_snapshots(job):
    """Get list of snapshots for a job"""
    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        result = subprocess.run(
            ['restic', 'snapshots', '--json'],
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        if result.returncode == 0:
            return json.loads(result.stdout)
        return []
    except:
        return []


def get_repo_stats(job):
    """Get repository statistics"""
    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        result = subprocess.run(
            ['restic', 'stats', '--json'],
            capture_output=True,
            text=True,
            env=env,
            timeout=120
        )

        if result.returncode == 0:
            return json.loads(result.stdout)
        return None
    except:
        return None


def schedule_job(job_id, job):
    """Schedule a backup job"""
    # Remove existing job if any
    try:
        scheduler.remove_job(job_id)
    except:
        pass

    if job.get('schedule_enabled'):
        cron = job.get('schedule_cron', '0 2 * * *')
        parts = cron.split()
        if len(parts) == 5:
            scheduler.add_job(
                run_backup,
                'cron',
                args=[job_id],
                id=job_id,
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4]
            )


# Routes
@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# API Authentication Routes
@app.route('/api/auth/login', methods=['POST'])
@csrf.exempt
def api_login():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    username = data.get('username')
    password = data.get('password')

    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'changeme')

    if username == admin_user and password == admin_pass:
        user = User(username)
        login_user(user)
        return jsonify({'user': {'id': username, 'username': username}})

    return jsonify({'error': 'Invalid credentials'}), 401


@app.route('/api/auth/logout', methods=['POST'])
@csrf.exempt
def api_logout():
    logout_user()
    return jsonify({'success': True})


@app.route('/api/auth/me')
def api_me():
    if current_user.is_authenticated:
        return jsonify({'id': current_user.id, 'username': current_user.id})
    return jsonify({'error': 'Not authenticated'}), 401


# API Routes
@app.route('/api/jobs')
@login_required
def api_jobs():
    jobs = load_jobs()
    return jsonify(jobs)


@app.route('/api/jobs/<job_id>/status')
@login_required
def api_job_status(job_id):
    jobs = load_jobs()
    if job_id in jobs:
        return jsonify({
            'status': jobs[job_id].get('status', 'unknown'),
            'last_run': jobs[job_id].get('last_run'),
            'last_success': jobs[job_id].get('last_success')
        })
    return jsonify({'error': 'Job not found'}), 404


@app.route('/api/jobs', methods=['POST'])
@login_required
@csrf.exempt
def api_create_job():
    """Create a new backup job"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    job_id = data.get('job_id', '').strip().lower().replace(' ', '-')
    if not job_id:
        return jsonify({'error': 'Job ID is required'}), 400

    jobs = load_jobs()
    if job_id in jobs:
        return jsonify({'error': 'Job ID already exists'}), 400

    # Resolve server and S3 config
    server_id = data.get('server_id')
    s3_config_id = data.get('s3_config_id')

    server = None
    s3_config = None

    if server_id:
        servers = load_servers()
        server = next((s for s in servers if s['id'] == server_id), None)
        if not server:
            return jsonify({'error': 'Server not found'}), 400

    if s3_config_id:
        s3_configs = load_s3_configs()
        s3_config = next((c for c in s3_configs if c['id'] == s3_config_id), None)
        if not s3_config:
            return jsonify({'error': 'S3 configuration not found'}), 400

    job = {
        'name': data.get('name', job_id),
        'server_id': server_id,
        's3_config_id': s3_config_id,
        # Store resolved values for backup execution
        'remote_host': f"{server['ssh_user']}@{server['host']}" if server else data.get('remote_host'),
        'ssh_port': server['ssh_port'] if server else int(data.get('ssh_port', 22)),
        'ssh_key': server['ssh_key'] if server else data.get('ssh_key', '/root/.ssh/id_rsa'),
        's3_endpoint': s3_config['endpoint'] if s3_config else data.get('s3_endpoint'),
        's3_bucket': s3_config['bucket'] if s3_config else data.get('s3_bucket'),
        's3_access_key': s3_config['access_key'] if s3_config else data.get('s3_access_key'),
        's3_secret_key': s3_config['secret_key'] if s3_config else data.get('s3_secret_key'),
        'directories': data.get('directories', []),
        'excludes': data.get('excludes', []),
        'restic_password': data.get('restic_password'),
        'backup_prefix': data.get('backup_prefix', job_id),
        'schedule_enabled': data.get('schedule_enabled', False),
        'schedule_cron': data.get('schedule_cron', '0 2 * * *'),
        'retention_hourly': int(data.get('retention_hourly', 24)),
        'retention_daily': int(data.get('retention_daily', 7)),
        'retention_weekly': int(data.get('retention_weekly', 4)),
        'retention_monthly': int(data.get('retention_monthly', 12)),
        'timeout': int(data.get('timeout', 7200)),
        'status': 'pending',
        'created_at': datetime.now().isoformat()
    }

    jobs[job_id] = job
    save_jobs(jobs)

    if job['schedule_enabled']:
        schedule_job(job_id, job)

    return jsonify({'success': True, 'job_id': job_id}), 201


@app.route('/api/jobs/<job_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_job(job_id):
    """Update an existing backup job"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    jobs = load_jobs()
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]

    # Resolve server and S3 config if provided
    server_id = data.get('server_id', job.get('server_id'))
    s3_config_id = data.get('s3_config_id', job.get('s3_config_id'))

    server = None
    s3_config = None

    if server_id:
        servers = load_servers()
        server = next((s for s in servers if s['id'] == server_id), None)
        if not server:
            return jsonify({'error': 'Server not found'}), 400

    if s3_config_id:
        s3_configs = load_s3_configs()
        s3_config = next((c for c in s3_configs if c['id'] == s3_config_id), None)
        if not s3_config:
            return jsonify({'error': 'S3 configuration not found'}), 400

    job.update({
        'name': data.get('name', job['name']),
        'server_id': server_id,
        's3_config_id': s3_config_id,
        # Store resolved values for backup execution
        'remote_host': f"{server['ssh_user']}@{server['host']}" if server else job.get('remote_host'),
        'ssh_port': server['ssh_port'] if server else job.get('ssh_port', 22),
        'ssh_key': server['ssh_key'] if server else job.get('ssh_key', '/root/.ssh/id_rsa'),
        's3_endpoint': s3_config['endpoint'] if s3_config else job.get('s3_endpoint'),
        's3_bucket': s3_config['bucket'] if s3_config else job.get('s3_bucket'),
        's3_access_key': s3_config['access_key'] if s3_config else job.get('s3_access_key'),
        'directories': data.get('directories', job['directories']),
        'excludes': data.get('excludes', job.get('excludes', [])),
        'backup_prefix': data.get('backup_prefix', job.get('backup_prefix', job_id)),
        'schedule_enabled': data.get('schedule_enabled', job.get('schedule_enabled', False)),
        'schedule_cron': data.get('schedule_cron', job.get('schedule_cron', '0 2 * * *')),
        'retention_hourly': int(data.get('retention_hourly', job.get('retention_hourly', 24))),
        'retention_daily': int(data.get('retention_daily', job.get('retention_daily', 7))),
        'retention_weekly': int(data.get('retention_weekly', job.get('retention_weekly', 4))),
        'retention_monthly': int(data.get('retention_monthly', job.get('retention_monthly', 12))),
        'timeout': int(data.get('timeout', job.get('timeout', 7200))),
        'updated_at': datetime.now().isoformat()
    })

    # Only update secrets if provided
    if s3_config:
        job['s3_secret_key'] = s3_config['secret_key']
    elif data.get('s3_secret_key'):
        job['s3_secret_key'] = data['s3_secret_key']
    if data.get('restic_password'):
        job['restic_password'] = data['restic_password']

    jobs[job_id] = job
    save_jobs(jobs)
    schedule_job(job_id, job)

    return jsonify({'success': True})


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_job(job_id):
    """Delete a backup job"""
    jobs = load_jobs()
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    del jobs[job_id]
    save_jobs(jobs)

    try:
        scheduler.remove_job(job_id)
    except:
        pass

    return jsonify({'success': True})


@app.route('/api/jobs/<job_id>/run', methods=['POST'])
@login_required
@csrf.exempt
def api_run_job(job_id):
    """Run a backup job"""
    jobs = load_jobs()
    if job_id not in jobs:
        return jsonify({'success': False, 'error': 'Job not found'}), 404

    # Run in background
    thread = threading.Thread(target=run_backup, args=[job_id])
    thread.start()

    return jsonify({'success': True, 'message': 'Backup started'})


@app.route('/api/jobs/<job_id>/snapshots')
@login_required
def api_job_snapshots(job_id):
    """Get snapshots for a job"""
    jobs = load_jobs()
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    snapshots = get_snapshots(job)
    stats = get_repo_stats(job)

    return jsonify({
        'snapshots': snapshots,
        'stats': stats
    })


@app.route('/api/history')
@login_required
def api_history():
    """Get backup history"""
    history = load_history()
    return jsonify(history)


# S3 Configuration API Routes
@app.route('/api/s3-configs', methods=['GET'])
@login_required
def api_get_s3_configs():
    """Get all S3 configurations"""
    configs = load_s3_configs()
    # Hide secret keys in response
    safe_configs = []
    for config in configs:
        safe_config = {**config}
        safe_config['secret_key'] = '********' if config.get('secret_key') else ''
        safe_configs.append(safe_config)
    return jsonify(safe_configs)


@app.route('/api/s3-configs', methods=['POST'])
@login_required
@csrf.exempt
def api_create_s3_config():
    """Create a new S3 configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required_fields = ['name', 'endpoint', 'bucket', 'access_key', 'secret_key']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    configs = load_s3_configs()

    new_config = {
        'id': generate_id(),
        'name': data['name'],
        'endpoint': data['endpoint'],
        'bucket': data['bucket'],
        'access_key': data['access_key'],
        'secret_key': data['secret_key'],
        'region': data.get('region', ''),
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }

    configs.append(new_config)
    save_s3_configs(configs)

    # Return safe version
    safe_config = {**new_config}
    safe_config['secret_key'] = '********'
    return jsonify(safe_config), 201


@app.route('/api/s3-configs/<config_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_s3_config(config_id):
    """Update an S3 configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    configs = load_s3_configs()
    config_index = next((i for i, c in enumerate(configs) if c['id'] == config_id), None)

    if config_index is None:
        return jsonify({'error': 'Configuration not found'}), 404

    config = configs[config_index]

    # Update fields
    config['name'] = data.get('name', config['name'])
    config['endpoint'] = data.get('endpoint', config['endpoint'])
    config['bucket'] = data.get('bucket', config['bucket'])
    config['access_key'] = data.get('access_key', config['access_key'])
    config['region'] = data.get('region', config.get('region', ''))
    config['updated_at'] = datetime.now().isoformat()

    # Only update secret_key if provided and not empty
    if data.get('secret_key'):
        config['secret_key'] = data['secret_key']

    configs[config_index] = config
    save_s3_configs(configs)

    # Return safe version
    safe_config = {**config}
    safe_config['secret_key'] = '********'
    return jsonify(safe_config)


@app.route('/api/s3-configs/<config_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_s3_config(config_id):
    """Delete an S3 configuration"""
    configs = load_s3_configs()
    config_index = next((i for i, c in enumerate(configs) if c['id'] == config_id), None)

    if config_index is None:
        return jsonify({'error': 'Configuration not found'}), 404

    configs.pop(config_index)
    save_s3_configs(configs)

    return jsonify({'success': True})


@app.route('/api/s3-configs/test', methods=['POST'])
@login_required
@csrf.exempt
def api_test_s3_connection():
    """Test S3 connection"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    endpoint = data.get('endpoint', '')
    bucket = data.get('bucket', '')
    access_key = data.get('access_key', '')
    secret_key = data.get('secret_key', '')
    region = data.get('region', 'us-east-1')

    if not all([endpoint, bucket, access_key, secret_key]):
        return jsonify({'error': 'Missing required fields'}), 400

    try:
        # Use rclone to test connection
        env = os.environ.copy()
        env['RCLONE_CONFIG_TEST_TYPE'] = 's3'
        env['RCLONE_CONFIG_TEST_PROVIDER'] = 'Other'
        env['RCLONE_CONFIG_TEST_ACCESS_KEY_ID'] = access_key
        env['RCLONE_CONFIG_TEST_SECRET_ACCESS_KEY'] = secret_key
        env['RCLONE_CONFIG_TEST_ENDPOINT'] = f'https://{endpoint}'
        env['RCLONE_CONFIG_TEST_REGION'] = region

        result = subprocess.run(
            ['rclone', 'lsd', f'test:{bucket}', '--max-depth', '1'],
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )

        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'Connection successful'})
        else:
            return jsonify({'error': result.stderr or 'Connection failed'}), 400

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Connection timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Server API Routes
@app.route('/api/servers', methods=['GET'])
@login_required
def api_get_servers():
    """Get all servers"""
    servers = load_servers()
    return jsonify(servers)


@app.route('/api/servers', methods=['POST'])
@login_required
@csrf.exempt
def api_create_server():
    """Create a new server"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required_fields = ['name', 'host', 'ssh_user']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    servers = load_servers()

    new_server = {
        'id': generate_id(),
        'name': data['name'],
        'host': data['host'],
        'ssh_port': int(data.get('ssh_port', 22)),
        'ssh_user': data['ssh_user'],
        'ssh_key': data.get('ssh_key', '/root/.ssh/id_rsa'),
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }

    servers.append(new_server)
    save_servers(servers)

    return jsonify(new_server), 201


@app.route('/api/servers/<server_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_server(server_id):
    """Update a server"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    servers = load_servers()
    server_index = next((i for i, s in enumerate(servers) if s['id'] == server_id), None)

    if server_index is None:
        return jsonify({'error': 'Server not found'}), 404

    server = servers[server_index]

    # Update fields
    server['name'] = data.get('name', server['name'])
    server['host'] = data.get('host', server['host'])
    server['ssh_port'] = int(data.get('ssh_port', server.get('ssh_port', 22)))
    server['ssh_user'] = data.get('ssh_user', server['ssh_user'])
    server['ssh_key'] = data.get('ssh_key', server.get('ssh_key', '/root/.ssh/id_rsa'))
    server['updated_at'] = datetime.now().isoformat()

    servers[server_index] = server
    save_servers(servers)

    return jsonify(server)


@app.route('/api/servers/<server_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_server(server_id):
    """Delete a server"""
    servers = load_servers()
    server_index = next((i for i, s in enumerate(servers) if s['id'] == server_id), None)

    if server_index is None:
        return jsonify({'error': 'Server not found'}), 404

    servers.pop(server_index)
    save_servers(servers)

    return jsonify({'success': True})


@app.route('/api/servers/test', methods=['POST'])
@login_required
@csrf.exempt
def api_test_server_connection():
    """Test SSH connection to server"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    host = data.get('host', '')
    ssh_port = int(data.get('ssh_port', 22))
    ssh_user = data.get('ssh_user', '')
    ssh_key = data.get('ssh_key', '/root/.ssh/id_rsa')

    if not all([host, ssh_user]):
        return jsonify({'error': 'Missing required fields'}), 400

    try:
        # Test SSH connection
        ssh_cmd = [
            'ssh', '-i', ssh_key,
            '-p', str(ssh_port),
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=10',
            f'{ssh_user}@{host}',
            'echo "Connection successful"'
        ]

        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'Connection successful'})
        else:
            return jsonify({'error': result.stderr or 'Connection failed'}), 400

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Connection timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Serve React frontend
@app.route('/assets/<path:filename>')
def serve_assets(filename):
    """Serve static assets from React build"""
    return send_from_directory(FRONTEND_DIST / 'assets', filename)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """Serve React frontend for all non-API routes"""
    # Skip API routes
    if path.startswith('api/'):
        return jsonify({'error': 'Not found'}), 404

    # Serve static files if they exist
    if path and (FRONTEND_DIST / path).exists():
        return send_from_directory(FRONTEND_DIST, path)

    # Serve index.html for all other routes (SPA routing)
    if (FRONTEND_DIST / 'index.html').exists():
        return send_from_directory(FRONTEND_DIST, 'index.html')

    # Fallback to old templates if React not built
    return redirect(url_for('login'))


# Initialize scheduled jobs on startup
def init_schedules():
    jobs = load_jobs()
    for job_id, job in jobs.items():
        if job.get('schedule_enabled'):
            schedule_job(job_id, job)


if __name__ == '__main__':
    init_schedules()
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('DEBUG', False))

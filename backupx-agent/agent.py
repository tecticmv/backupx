#!/usr/bin/env python3
"""
BackupX Agent
Lightweight agent for remote backup operations
"""

import os
import sys
import json
import subprocess
import logging
import hashlib
import hmac
import shlex
import re
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify

# =============================================================================
# Configuration
# =============================================================================

app = Flask(__name__)

# Agent configuration from environment
AGENT_API_KEY = os.environ.get('AGENT_API_KEY', '')
AGENT_PORT = int(os.environ.get('AGENT_PORT', 8090))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
AGENT_NAME = os.environ.get('AGENT_NAME', 'backupx-agent')

# Allowed backup paths (comma-separated, empty = allow all)
ALLOWED_PATHS = os.environ.get('ALLOWED_PATHS', '')
ALLOWED_PATHS_LIST = [p.strip() for p in ALLOWED_PATHS.split(',') if p.strip()] if ALLOWED_PATHS else []

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('backupx-agent')

# =============================================================================
# Security
# =============================================================================

def verify_api_key(f):
    """Decorator to verify API key authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AGENT_API_KEY:
            logger.error("AGENT_API_KEY not configured - rejecting all requests")
            return jsonify({'error': 'Agent not configured'}), 500

        auth_header = request.headers.get('X-API-Key', '')
        if not auth_header:
            auth_header = request.headers.get('Authorization', '').replace('Bearer ', '')

        if not hmac.compare_digest(auth_header, AGENT_API_KEY):
            logger.warning(f"Invalid API key from {request.remote_addr}")
            return jsonify({'error': 'Invalid API key'}), 401

        return f(*args, **kwargs)
    return decorated


def is_path_allowed(path: str) -> bool:
    """Check if path is in allowed paths list"""
    if not ALLOWED_PATHS_LIST:
        return True  # No restrictions

    # Normalize path
    path = os.path.normpath(path)

    for allowed in ALLOWED_PATHS_LIST:
        allowed = os.path.normpath(allowed)
        if path.startswith(allowed):
            return True
    return False


def sanitize_error(error: str, max_length: int = 500) -> str:
    """Remove sensitive data from error messages"""
    if not error:
        return "Unknown error"
    sanitized = re.sub(r'(password|secret|key|token)[\s]*[=:]\s*[^\s]+', r'\1=***', error, flags=re.IGNORECASE)
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + '...'
    return sanitized

# =============================================================================
# API Endpoints
# =============================================================================

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint (no auth required)"""
    return jsonify({
        'status': 'healthy',
        'agent': AGENT_NAME,
        'version': '1.0.0',
        'timestamp': datetime.now().isoformat()
    })


@app.route('/info', methods=['GET'])
@verify_api_key
def info():
    """Get agent information"""
    # Check for restic
    restic_version = None
    try:
        result = subprocess.run(['restic', 'version'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            restic_version = result.stdout.strip().split('\n')[0]
    except:
        pass

    # Check for mysqldump
    mysqldump_available = False
    try:
        result = subprocess.run(['which', 'mysqldump'], capture_output=True, timeout=10)
        mysqldump_available = result.returncode == 0
    except:
        pass

    return jsonify({
        'agent': AGENT_NAME,
        'version': '1.0.0',
        'hostname': os.uname().nodename,
        'restic_version': restic_version,
        'mysqldump_available': mysqldump_available,
        'allowed_paths': ALLOWED_PATHS_LIST or ['*'],
        'timestamp': datetime.now().isoformat()
    })


@app.route('/backup/filesystem', methods=['POST'])
@verify_api_key
def backup_filesystem():
    """Execute filesystem backup"""
    data = request.get_json()

    if not data:
        logger.error("No JSON data provided in request")
        return jsonify({'error': 'No data provided'}), 400

    # Log request details (without sensitive data)
    logger.info(f"Received filesystem backup request: directories={data.get('directories')}, "
                f"s3_endpoint={data.get('s3_endpoint')}, s3_bucket={data.get('s3_bucket')}, "
                f"backup_prefix={data.get('backup_prefix')}")

    # Required fields
    required = ['directories', 's3_endpoint', 's3_bucket', 's3_access_key', 's3_secret_key', 'restic_password', 'backup_prefix']
    missing = [f for f in required if not data.get(f)]
    if missing:
        logger.error(f"Missing required fields: {missing}")
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    directories = data['directories']
    if isinstance(directories, str):
        directories = [directories]

    # Validate paths
    for directory in directories:
        if not is_path_allowed(directory):
            logger.error(f"Path not allowed: {directory}. Allowed paths: {ALLOWED_PATHS_LIST or ['*']}")
            return jsonify({'error': f'Path not allowed: {directory}'}), 403
        if not os.path.exists(directory):
            logger.error(f"Path does not exist in container: {directory}")
            return jsonify({'error': f'Path does not exist: {directory}'}), 400

    excludes = data.get('excludes', [])
    if isinstance(excludes, str):
        excludes = [excludes]

    logger.info(f"Starting filesystem backup: {directories}")
    start_time = datetime.now()

    try:
        # Set environment for restic
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = data['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = data['s3_secret_key']
        env['RESTIC_PASSWORD'] = data['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{data['s3_endpoint']}/{data['s3_bucket']}/{data['backup_prefix']}"

        # Build restic command
        cmd = ['restic', 'backup', '--compression', 'auto', '--tag', 'automated', '--json']

        # Add insecure TLS flag if skip_ssl_verify is enabled
        if data.get('skip_ssl_verify'):
            cmd.append('--insecure-tls')

        for exclude in excludes:
            cmd.extend(['--exclude', exclude])

        cmd.extend(directories)

        # Execute backup
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=data.get('timeout', 7200)
        )

        duration = (datetime.now() - start_time).total_seconds()

        if result.returncode == 0:
            logger.info(f"Backup completed successfully in {duration:.1f}s")

            # Parse JSON output for summary
            summary = None
            for line in result.stdout.strip().split('\n'):
                try:
                    parsed = json.loads(line)
                    if parsed.get('message_type') == 'summary':
                        summary = parsed
                except:
                    pass

            return jsonify({
                'success': True,
                'message': 'Backup completed successfully',
                'duration': duration,
                'summary': summary
            })
        else:
            error_msg = sanitize_error(result.stderr or result.stdout)
            logger.error(f"Backup failed: {error_msg}")
            return jsonify({
                'success': False,
                'error': error_msg,
                'duration': duration
            }), 500

    except subprocess.TimeoutExpired:
        logger.error("Backup timed out")
        return jsonify({'success': False, 'error': 'Backup timed out'}), 500
    except Exception as e:
        logger.exception("Backup error")
        return jsonify({'success': False, 'error': sanitize_error(str(e))}), 500


@app.route('/backup/database', methods=['POST'])
@verify_api_key
def backup_database():
    """Execute MySQL database backup"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Required fields
    required = ['db_host', 'db_user', 'db_password', 's3_endpoint', 's3_bucket', 's3_access_key', 's3_secret_key', 'restic_password', 'backup_prefix']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    db_host = data['db_host']
    db_port = int(data.get('db_port', 3306))
    db_user = data['db_user']
    db_password = data['db_password']
    databases = data.get('databases', '*')

    logger.info(f"Starting database backup: {databases}")
    start_time = datetime.now()

    try:
        # Set environment for restic
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = data['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = data['s3_secret_key']
        env['RESTIC_PASSWORD'] = data['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{data['s3_endpoint']}/{data['s3_bucket']}/{data['backup_prefix']}"

        # Create temp file for backup
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_file = f'/tmp/mysql_backup_{timestamp}.sql.gz'

        # Build mysqldump command (using list format to avoid shell injection)
        mysqldump_cmd = [
            'mysqldump',
            '-h', db_host,
            '-P', str(db_port),
            '-u', db_user,
            f'-p{db_password}',
            '--single-transaction',
            '--routines',
            '--triggers'
        ]

        if databases == '*':
            mysqldump_cmd.append('--all-databases')
        else:
            db_list = [db.strip() for db in databases.split(',') if db.strip()]
            mysqldump_cmd.append('--databases')
            mysqldump_cmd.extend(db_list)

        # Run mysqldump and gzip separately (no shell=True)
        # First run mysqldump, pipe stdout to gzip
        try:
            mysqldump_proc = subprocess.Popen(
                mysqldump_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            gzip_proc = subprocess.Popen(
                ['gzip'],
                stdin=mysqldump_proc.stdout,
                stdout=open(backup_file, 'wb'),
                stderr=subprocess.PIPE
            )

            # Allow mysqldump to receive SIGPIPE if gzip exits
            mysqldump_proc.stdout.close()

            # Wait for both processes with timeout
            timeout_seconds = data.get('timeout', 3600)
            gzip_proc.wait(timeout=timeout_seconds)
            mysqldump_proc.wait(timeout=10)  # Should already be done

            if mysqldump_proc.returncode != 0:
                error_msg = sanitize_error(mysqldump_proc.stderr.read().decode() if mysqldump_proc.stderr else '')
                logger.error(f"mysqldump failed: {error_msg}")
                return jsonify({'success': False, 'error': f'mysqldump failed: {error_msg}'}), 500

            if gzip_proc.returncode != 0:
                error_msg = sanitize_error(gzip_proc.stderr.read().decode() if gzip_proc.stderr else '')
                logger.error(f"gzip failed: {error_msg}")
                return jsonify({'success': False, 'error': f'gzip failed: {error_msg}'}), 500

        except subprocess.TimeoutExpired:
            # Kill both processes on timeout
            mysqldump_proc.kill()
            gzip_proc.kill()
            raise

        # Backup to restic
        restic_cmd = ['restic', 'backup', '--compression', 'auto', '--tag', 'automated', '--tag', 'mysql-backup', '--json']

        # Add insecure TLS flag if skip_ssl_verify is enabled
        if data.get('skip_ssl_verify'):
            restic_cmd.append('--insecure-tls')

        restic_cmd.append(backup_file)

        restic_result = subprocess.run(
            restic_cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=data.get('timeout', 3600)
        )

        # Cleanup temp file
        try:
            os.remove(backup_file)
        except:
            pass

        duration = (datetime.now() - start_time).total_seconds()

        if restic_result.returncode == 0:
            logger.info(f"Database backup completed successfully in {duration:.1f}s")
            return jsonify({
                'success': True,
                'message': f'Database backup completed successfully ({databases})',
                'duration': duration
            })
        else:
            error_msg = sanitize_error(restic_result.stderr or restic_result.stdout)
            logger.error(f"Restic backup failed: {error_msg}")
            return jsonify({
                'success': False,
                'error': error_msg,
                'duration': duration
            }), 500

    except subprocess.TimeoutExpired:
        logger.error("Database backup timed out")
        return jsonify({'success': False, 'error': 'Database backup timed out'}), 500
    except Exception as e:
        logger.exception("Database backup error")
        return jsonify({'success': False, 'error': sanitize_error(str(e))}), 500


@app.route('/snapshots', methods=['POST'])
@verify_api_key
def list_snapshots():
    """List snapshots in repository"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = ['s3_endpoint', 's3_bucket', 's3_access_key', 's3_secret_key', 'restic_password', 'backup_prefix']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = data['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = data['s3_secret_key']
        env['RESTIC_PASSWORD'] = data['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{data['s3_endpoint']}/{data['s3_bucket']}/{data['backup_prefix']}"

        cmd = ['restic', 'snapshots', '--json']
        if data.get('skip_ssl_verify'):
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        if result.returncode == 0:
            snapshots = json.loads(result.stdout) if result.stdout else []
            return jsonify({'success': True, 'snapshots': snapshots})
        else:
            return jsonify({'success': False, 'error': sanitize_error(result.stderr)}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': sanitize_error(str(e))}), 500


@app.route('/stats', methods=['POST'])
@verify_api_key
def repo_stats():
    """Get repository statistics"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = ['s3_endpoint', 's3_bucket', 's3_access_key', 's3_secret_key', 'restic_password', 'backup_prefix']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = data['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = data['s3_secret_key']
        env['RESTIC_PASSWORD'] = data['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{data['s3_endpoint']}/{data['s3_bucket']}/{data['backup_prefix']}"

        cmd = ['restic', 'stats', '--json']
        if data.get('skip_ssl_verify'):
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=120
        )

        if result.returncode == 0:
            stats = json.loads(result.stdout) if result.stdout else {}
            return jsonify({'success': True, 'stats': stats})
        else:
            return jsonify({'success': False, 'error': sanitize_error(result.stderr)}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': sanitize_error(str(e))}), 500


@app.route('/init', methods=['POST'])
@verify_api_key
def init_repo():
    """Initialize restic repository"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = ['s3_endpoint', 's3_bucket', 's3_access_key', 's3_secret_key', 'restic_password', 'backup_prefix']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = data['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = data['s3_secret_key']
        env['RESTIC_PASSWORD'] = data['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{data['s3_endpoint']}/{data['s3_bucket']}/{data['backup_prefix']}"

        cmd = ['restic', 'init']
        if data.get('skip_ssl_verify'):
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'Repository initialized'})
        elif 'already initialized' in result.stderr.lower() or 'already exists' in result.stderr.lower():
            return jsonify({'success': True, 'message': 'Repository already initialized'})
        else:
            return jsonify({'success': False, 'error': sanitize_error(result.stderr)}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': sanitize_error(str(e))}), 500


# =============================================================================
# Main
# =============================================================================

if __name__ == '__main__':
    if not AGENT_API_KEY:
        logger.error("AGENT_API_KEY environment variable is required!")
        sys.exit(1)

    logger.info(f"Starting BackupX Agent '{AGENT_NAME}' on port {AGENT_PORT}")
    logger.info(f"Allowed paths: {ALLOWED_PATHS_LIST or ['*']}")

    # Use waitress for production
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=AGENT_PORT)
    except ImportError:
        # Fallback to Flask dev server
        app.run(host='0.0.0.0', port=AGENT_PORT, debug=False)

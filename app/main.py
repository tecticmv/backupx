#!/usr/bin/env python3
"""
Backup Manager UI
A web interface for managing restic backups
"""

import os
import sys
import json
import subprocess
import threading
import logging
import smtplib
import ssl
import shlex
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, redirect, url_for, jsonify, send_from_directory, g
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
import yaml
import humanize

# =============================================================================
# Logging Configuration
# =============================================================================

def setup_logging():
    """Configure logging for production"""
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Reduce verbosity of some loggers
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)

    return logging.getLogger(__name__)

logger = setup_logging()


# =============================================================================
# Environment Validation
# =============================================================================

def is_production_mode() -> bool:
    """Check if running in production mode"""
    return os.environ.get('FLASK_ENV', 'production').lower() == 'production' and \
           os.environ.get('ENVIRONMENT', 'production').lower() != 'development'


def validate_environment():
    """Validate required environment variables"""
    errors = []
    warnings = []
    production = is_production_mode()

    # Check SECRET_KEY
    secret_key = os.environ.get('SECRET_KEY', '')
    if not secret_key or secret_key == 'change-this-secret-key':
        if production:
            errors.append("CRITICAL: SECRET_KEY must be set in production. Generate with: python -c \"import secrets; print(secrets.token_hex(32))\"")
        else:
            warnings.append("SECRET_KEY is not set or using default value. Set a strong random key in production.")

    # Check admin credentials
    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'changeme')

    if admin_user == 'admin':
        if production:
            errors.append("CRITICAL: ADMIN_USERNAME must be changed from default 'admin' in production.")
        else:
            warnings.append("ADMIN_USERNAME is using default value 'admin'. Consider changing it.")

    if admin_pass == 'changeme':
        if production:
            errors.append("CRITICAL: ADMIN_PASSWORD must be changed from default in production.")
        else:
            warnings.append("ADMIN_PASSWORD is using default value. Set a strong password in production.")

    # Check for minimum password length in production
    if len(admin_pass) < 12 and production:
        errors.append("CRITICAL: ADMIN_PASSWORD must be at least 12 characters in production.")

    # Log warnings and errors
    for warning in warnings:
        logger.warning(warning)
    for error in errors:
        logger.error(error)

    # In production, fail startup if critical issues exist
    if errors and production:
        logger.critical("Application cannot start with default credentials in production mode.")
        logger.critical("Set ENVIRONMENT=development to bypass these checks during development.")
        raise SystemExit(1)

    return len(warnings) == 0 and len(errors) == 0


# =============================================================================
# Credential Encryption
# =============================================================================

# Encryption key derived from SECRET_KEY (for encrypting stored credentials)
_fernet = None


def utc_now():
    """Get current UTC time with timezone info"""
    return datetime.now(timezone.utc)


def utc_isoformat():
    """Get current UTC time as ISO format string"""
    return utc_now().isoformat()


def get_fernet():
    """Get Fernet instance for encryption/decryption"""
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        import base64
        import hashlib
        # Derive a 32-byte key from SECRET_KEY using PBKDF2 (more secure than plain SHA-256)
        secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')
        # Use a fixed salt derived from the secret key itself for deterministic key derivation
        # This ensures the same SECRET_KEY always produces the same encryption key
        salt = hashlib.sha256(b'backupx-credential-salt:' + secret_key.encode()).digest()[:16]
        key = hashlib.pbkdf2_hmac('sha256', secret_key.encode(), salt, iterations=100000)
        _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a credential for storage"""
    if not plaintext:
        return ''
    try:
        fernet = get_fernet()
        return fernet.encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption error: {e}")
        # Return original if encryption fails (for backwards compatibility during migration)
        return plaintext


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a stored credential"""
    if not ciphertext:
        return ''
    try:
        fernet = get_fernet()
        return fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        # If decryption fails, assume it's a legacy plaintext value
        return ciphertext


def is_encrypted(value: str) -> bool:
    """Check if a value appears to be Fernet-encrypted"""
    if not value:
        return False
    # Fernet tokens start with 'gAAAAA' (base64-encoded version byte)
    return value.startswith('gAAAAA')


# Hash the admin password at startup for secure comparison
_admin_password_hash = None
_admin_password_hash_loaded = False

def get_admin_password_hash():
    """Get hashed admin password - checks database first, then env var.
    Always re-checks database to ensure password changes are picked up."""
    global _admin_password_hash, _admin_password_hash_loaded

    # Try to get saved hash from database (always check on each call for changes)
    try:
        saved_hash = get_app_setting('admin_password_hash')
        if saved_hash:
            _admin_password_hash = saved_hash
            _admin_password_hash_loaded = True
            return _admin_password_hash
    except Exception:
        pass  # Database not ready yet, fall back to cached or env var

    # If we already loaded from env, return cached value
    if _admin_password_hash_loaded and _admin_password_hash:
        return _admin_password_hash

    # Fall back to environment variable
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'changeme')
    _admin_password_hash = generate_password_hash(admin_pass)
    _admin_password_hash_loaded = True
    return _admin_password_hash


def refresh_admin_password_hash():
    """Force refresh of admin password hash from database."""
    global _admin_password_hash, _admin_password_hash_loaded
    _admin_password_hash = None
    _admin_password_hash_loaded = False
    return get_admin_password_hash()

# Validate environment on startup
env_valid = validate_environment()
if not env_valid:
    logger.warning("Application starting with configuration warnings. Review settings for production use.")


# Initialize Flask app
FRONTEND_DIST = Path(__file__).parent.parent / 'frontend' / 'dist'
app = Flask(__name__, static_folder='../static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')

# CSRF Protection
csrf = CSRFProtect(app)

# Rate Limiting
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Get rate limit storage URI from environment (default to memory for development)
# For production, use Redis: redis://localhost:6379 or postgresql://... for DB-backed storage
rate_limit_storage = os.environ.get('RATE_LIMIT_STORAGE', 'memory://')
if rate_limit_storage == 'memory://' and is_production_mode():
    logger.warning("Using in-memory rate limiting storage. For production, set RATE_LIMIT_STORAGE to redis:// or memcached://")

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=rate_limit_storage,
)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


# =============================================================================
# Security Headers Middleware
# =============================================================================

# CSP nonce for inline scripts (regenerated per request for security)
import secrets

def generate_csp_nonce():
    """Generate a random nonce for CSP"""
    return secrets.token_urlsafe(16)


@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    # Prevent clickjacking
    response.headers['X-Frame-Options'] = 'DENY'
    # Prevent MIME type sniffing
    response.headers['X-Content-Type-Options'] = 'nosniff'
    # Enable XSS filter (legacy, but still useful)
    response.headers['X-XSS-Protection'] = '1; mode=block'
    # Referrer policy
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # Permissions policy (restrict browser features)
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    # HSTS - enforce HTTPS (only in production and when served over HTTPS)
    if is_production_mode():
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    # Content Security Policy
    # Note: Vite/React apps require 'unsafe-inline' for styles due to CSS-in-JS
    # In production, we use a stricter policy with nonces for scripts where possible
    # The unsafe-inline for scripts is required by the Vite bundled React app
    csp_policy = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "  # Required for React/Vite
        "style-src 'self' 'unsafe-inline'; "   # Required for CSS-in-JS
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none';"
    )
    response.headers['Content-Security-Policy'] = csp_policy

    return response

# Paths
CONFIG_DIR = Path('/app/config')
LOGS_DIR = Path('/app/logs')
DATA_DIR = Path('/app/data')
# Legacy JSON file paths (for migration)
JOBS_FILE = DATA_DIR / 'jobs.json'
HISTORY_FILE = DATA_DIR / 'history.json'
S3_CONFIGS_FILE = DATA_DIR / 's3_configs.json'
SERVERS_FILE = DATA_DIR / 'servers.json'
DB_CONFIGS_FILE = DATA_DIR / 'db_configs.json'

# Ensure directories exist
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Input Validation
# =============================================================================

def validate_hostname(hostname: str) -> bool:
    """Validate hostname or IP address"""
    if not hostname or len(hostname) > 255:
        return False
    # Allow IPv4, IPv6, or valid hostname
    hostname_pattern = re.compile(
        r'^('
        r'([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z]{2,}|'  # hostname
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|'  # IPv4
        r'\[?[a-fA-F0-9:]+\]?'  # IPv6
        r')$'
    )
    return bool(hostname_pattern.match(hostname))


def validate_port(port: int) -> bool:
    """Validate port number"""
    return isinstance(port, int) and 1 <= port <= 65535


def validate_path(path: str) -> bool:
    """Validate filesystem path (no path traversal)"""
    if not path:
        return False
    # Block path traversal attempts
    if '..' in path or path.startswith('~'):
        return False
    return path.startswith('/')


def validate_cron(cron_expr: str) -> bool:
    """Validate cron expression format"""
    if not cron_expr:
        return False
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False
    # Basic validation - each part should be numeric, *, or contain valid cron chars
    cron_pattern = re.compile(r'^[\d\*,\-/]+$')
    return all(cron_pattern.match(p) for p in parts)


def validate_s3_endpoint(endpoint: str) -> bool:
    """Validate S3 endpoint format"""
    if not endpoint:
        return False
    # Allow domain:port or just domain
    endpoint_pattern = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9\-\.]*[a-zA-Z0-9](:\d+)?$')
    return bool(endpoint_pattern.match(endpoint))


def validate_bucket_name(name: str) -> bool:
    """Validate S3 bucket name"""
    if not name or len(name) < 3 or len(name) > 63:
        return False
    # S3 bucket naming rules
    bucket_pattern = re.compile(r'^[a-z0-9][a-z0-9\-\.]*[a-z0-9]$')
    return bool(bucket_pattern.match(name))

# Scheduler - initialized with TZ env var, will be reconfigured with DB setting in init_app()
scheduler = BackgroundScheduler(timezone=os.environ.get('TZ', 'UTC'))
scheduler.start()


def reinit_scheduler_with_db_timezone():
    """Re-initialize scheduler with timezone from database (called after DB is ready)"""
    global scheduler
    try:
        from .db import get_database
        db = get_database()
        result = db.fetchone('SELECT value FROM app_settings WHERE key = %s', ('timezone',))
        db_tz = result['value'] if result else None
        timezone = db_tz or os.environ.get('TZ', 'UTC')

        if timezone != scheduler.timezone.zone:
            logger.info(f"Reinitializing scheduler with timezone: {timezone}")
            scheduler.shutdown(wait=False)
            scheduler = BackgroundScheduler(timezone=timezone)
            scheduler.start()
    except Exception as e:
        logger.debug(f"Could not reinit scheduler timezone from DB: {e}")


# =============================================================================
# Database Setup
# =============================================================================

def get_db():
    """Get the PostgreSQL database backend"""
    from .db.factory import get_database
    return get_database()


def get_db_connection():
    """Get the PostgreSQL database backend (alias for get_db for compatibility)"""
    return get_db()


def init_db():
    """Initialize PostgreSQL database schema via the database backend"""
    from .db import init_database
    init_database()
    logger.info("PostgreSQL database initialized")


def migrate_json_to_db():
    """Migrate legacy JSON data to PostgreSQL (runs once on startup)"""
    from .db.migrate import migrate_json_to_database
    db = get_db()
    migrate_json_to_database(str(DATA_DIR), db)
    logger.info("Migration complete!")


# User class for authentication
class User(UserMixin):
    def __init__(self, id):
        self.id = id


@login_manager.user_loader
def load_user(user_id):
    if user_id == os.environ.get('ADMIN_USERNAME', 'admin'):
        return User(user_id)
    return None


# =============================================================================
# Data Access Functions
# =============================================================================

def generate_id():
    """Generate a unique ID"""
    import uuid
    return str(uuid.uuid4())[:8]


# --- Jobs ---

def _decrypt_job(job):
    """Decrypt sensitive fields in job config"""
    if job:
        job['s3_access_key'] = decrypt_credential(job.get('s3_access_key', '') or '')
        job['s3_secret_key'] = decrypt_credential(job.get('s3_secret_key', '') or '')
        job['restic_password'] = decrypt_credential(job.get('restic_password', '') or '')
    return job


def load_jobs():
    """Load all backup jobs from database"""
    conn = get_db_connection()
    rows = conn.fetchall('SELECT * FROM jobs')


    jobs = {}
    for row in rows:
        job = _decrypt_job(dict(row))
        # Convert JSON strings to lists
        job['directories'] = json.loads(job['directories'] or '[]')
        job['excludes'] = json.loads(job['excludes'] or '[]')
        # Convert integer to boolean
        job['schedule_enabled'] = bool(job['schedule_enabled'])
        # Remove the 'id' from the job dict since it's the key
        job_id = job.pop('id')
        jobs[job_id] = job
    return jobs


def get_job(job_id):
    """Get a single job by ID"""
    conn = get_db_connection()
    row = conn.fetchone('SELECT * FROM jobs WHERE id = ?', (job_id,))


    if row:
        job = _decrypt_job(dict(row))
        job['directories'] = json.loads(job['directories'] or '[]')
        job['excludes'] = json.loads(job['excludes'] or '[]')
        job['schedule_enabled'] = bool(job['schedule_enabled'])
        return job
    return None


def save_job(job_id, job):
    """Save a job to database (insert or update)"""
    conn = get_db_connection()

    # Encrypt sensitive fields
    encrypted_s3_access_key = encrypt_credential(job.get('s3_access_key', '') or '')
    encrypted_s3_secret_key = encrypt_credential(job.get('s3_secret_key', '') or '')
    encrypted_restic_password = encrypt_credential(job.get('restic_password', '') or '')

    # Check if job exists
    exists = conn.fetchone('SELECT 1 FROM jobs WHERE id = ?', (job_id,))

    if exists:
        conn.execute('''
            UPDATE jobs SET name=?, backup_type=?, server_id=?, s3_config_id=?, remote_host=?, ssh_port=?, ssh_key=?,
                s3_endpoint=?, s3_bucket=?, s3_access_key=?, s3_secret_key=?, directories=?, excludes=?, database_config_id=?,
                restic_password=?, backup_prefix=?, schedule_enabled=?, schedule_cron=?, retention_hourly=?, retention_daily=?,
                retention_weekly=?, retention_monthly=?, timeout=?, status=?, updated_at=?, last_run=?, last_success=?
            WHERE id=?
        ''', (job['name'], job.get('backup_type', 'filesystem'), job.get('server_id'), job.get('s3_config_id'),
              job.get('remote_host'), job.get('ssh_port', 22), job.get('ssh_key'),
              job.get('s3_endpoint'), job.get('s3_bucket'), encrypted_s3_access_key, encrypted_s3_secret_key,
              json.dumps(job.get('directories', [])), json.dumps(job.get('excludes', [])), job.get('database_config_id'),
              encrypted_restic_password, job.get('backup_prefix'), bool(job.get('schedule_enabled')),
              job.get('schedule_cron', '0 2 * * *'), job.get('retention_hourly', 24), job.get('retention_daily', 7),
              job.get('retention_weekly', 4), job.get('retention_monthly', 12), job.get('timeout', 7200),
              job.get('status', 'pending'), job.get('updated_at'), job.get('last_run'), job.get('last_success'),
              job_id))
    else:
        conn.execute('''
            INSERT INTO jobs (id, name, backup_type, server_id, s3_config_id, remote_host, ssh_port, ssh_key,
                s3_endpoint, s3_bucket, s3_access_key, s3_secret_key, directories, excludes, database_config_id,
                restic_password, backup_prefix, schedule_enabled, schedule_cron, retention_hourly, retention_daily,
                retention_weekly, retention_monthly, timeout, status, created_at, updated_at, last_run, last_success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (job_id, job['name'], job.get('backup_type', 'filesystem'), job.get('server_id'), job.get('s3_config_id'),
              job.get('remote_host'), job.get('ssh_port', 22), job.get('ssh_key'),
              job.get('s3_endpoint'), job.get('s3_bucket'), encrypted_s3_access_key, encrypted_s3_secret_key,
              json.dumps(job.get('directories', [])), json.dumps(job.get('excludes', [])), job.get('database_config_id'),
              encrypted_restic_password, job.get('backup_prefix'), bool(job.get('schedule_enabled')),
              job.get('schedule_cron', '0 2 * * *'), job.get('retention_hourly', 24), job.get('retention_daily', 7),
              job.get('retention_weekly', 4), job.get('retention_monthly', 12), job.get('timeout', 7200),
              job.get('status', 'pending'), job.get('created_at', utc_isoformat()), job.get('updated_at'),
              job.get('last_run'), job.get('last_success')))

    conn.commit()



def delete_job_from_db(job_id):
    """Delete a job from database"""
    conn = get_db_connection()
    conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
    conn.commit()



def update_job_status(job_id, status, last_run=None, last_success=None):
    """Update job status efficiently"""
    conn = get_db_connection()
    if last_success:
        conn.execute('UPDATE jobs SET status=?, last_run=?, last_success=?, updated_at=? WHERE id=?',
                     (status, last_run, last_success, utc_isoformat(), job_id))
    elif last_run:
        conn.execute('UPDATE jobs SET status=?, last_run=?, updated_at=? WHERE id=?',
                     (status, last_run, utc_isoformat(), job_id))
    else:
        conn.execute('UPDATE jobs SET status=?, updated_at=? WHERE id=?',
                     (status, utc_isoformat(), job_id))
    conn.commit()



def update_job_progress(job_id, progress, message):
    """Update job progress during backup execution"""
    conn = get_db_connection()
    conn.execute('UPDATE jobs SET progress=?, progress_message=?, updated_at=? WHERE id=?',
                 (progress, message, utc_isoformat(), job_id))
    conn.commit()



# --- History ---

def load_history():
    """Load backup history from database"""
    conn = get_db_connection()
    rows = conn.fetchall('SELECT * FROM history ORDER BY timestamp DESC LIMIT 100')

    return [dict(row) for row in rows]


def add_history(job_id, job_name, status, message, duration=0):
    """Add entry to backup history"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO history (timestamp, job_id, job_name, status, message, duration)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (utc_isoformat(), job_id, job_name, status, message, duration))

    # Keep only last 100 entries
    conn.execute('''
        DELETE FROM history WHERE id NOT IN (
            SELECT id FROM history ORDER BY timestamp DESC LIMIT 100
        )
    ''')
    conn.commit()



# --- S3 Configs ---

def _decrypt_s3_config(config):
    """Decrypt sensitive fields in S3 config"""
    if config:
        config['access_key'] = decrypt_credential(config.get('access_key', ''))
        config['secret_key'] = decrypt_credential(config.get('secret_key', ''))
        config['skip_ssl_verify'] = bool(config.get('skip_ssl_verify', 0))
    return config


def load_s3_configs():
    """Load S3 configurations from database"""
    conn = get_db_connection()
    rows = conn.fetchall('SELECT * FROM s3_configs')

    return [_decrypt_s3_config(dict(row)) for row in rows]


def get_s3_config(config_id):
    """Get a single S3 config by ID"""
    conn = get_db_connection()
    row = conn.fetchone('SELECT * FROM s3_configs WHERE id = ?', (config_id,))

    return _decrypt_s3_config(dict(row)) if row else None


def create_s3_config(config):
    """Create a new S3 config"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, skip_ssl_verify, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (config['id'], config['name'], config['endpoint'], config['bucket'],
          encrypt_credential(config['access_key']), encrypt_credential(config['secret_key']),
          config.get('region', ''), bool(config.get('skip_ssl_verify')),
          config.get('status', 'active'), config.get('created_at', utc_isoformat()), config.get('updated_at')))
    conn.commit()



def update_s3_config(config_id, config):
    """Update an S3 config"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE s3_configs SET name=?, endpoint=?, bucket=?, access_key=?, secret_key=?, region=?, skip_ssl_verify=?, status=?, updated_at=?
        WHERE id=?
    ''', (config['name'], config['endpoint'], config['bucket'],
          encrypt_credential(config['access_key']), encrypt_credential(config['secret_key']),
          config.get('region', ''), bool(config.get('skip_ssl_verify')),
          config.get('status', 'active'), utc_isoformat(), config_id))
    conn.commit()



def delete_s3_config(config_id):
    """Delete an S3 config"""
    conn = get_db_connection()
    conn.execute('DELETE FROM s3_configs WHERE id = ?', (config_id,))
    conn.commit()



# --- Servers ---

def _decrypt_server(server):
    """Return server config (no sensitive fields to decrypt for SSH-only)"""
    return server


def load_servers():
    """Load servers from database"""
    conn = get_db_connection()
    rows = conn.fetchall('SELECT * FROM servers')

    return [_decrypt_server(dict(row)) for row in rows]


def get_server(server_id):
    """Get a single server by ID"""
    conn = get_db_connection()
    row = conn.fetchone('SELECT * FROM servers WHERE id = ?', (server_id,))

    return _decrypt_server(dict(row)) if row else None


def create_server(server):
    """Create a new server"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO servers (id, name, host, ssh_port, ssh_user, ssh_key, ssh_auth_type, ssh_password, ssh_key_content, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (server['id'], server['name'], server['host'],
          server.get('ssh_port', 22), server.get('ssh_user'), server.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
          server.get('ssh_auth_type', 'key_path'), server.get('ssh_password'), server.get('ssh_key_content'),
          server.get('status', 'active'), server.get('created_at', utc_isoformat()), server.get('updated_at')))
    conn.commit()



def update_server_in_db(server_id, server):
    """Update a server"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE servers SET name=?, host=?, ssh_port=?, ssh_user=?, ssh_key=?, ssh_auth_type=?, ssh_password=?, ssh_key_content=?, status=?, updated_at=?
        WHERE id=?
    ''', (server['name'], server['host'],
          server.get('ssh_port', 22), server.get('ssh_user'), server.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
          server.get('ssh_auth_type', 'key_path'), server.get('ssh_password'), server.get('ssh_key_content'),
          server.get('status', 'active'), utc_isoformat(), server_id))
    conn.commit()



def delete_server_from_db(server_id):
    """Delete a server"""
    conn = get_db_connection()
    conn.execute('DELETE FROM servers WHERE id = ?', (server_id,))
    conn.commit()



# --- Database Configs ---

def _decrypt_db_config(config):
    """Decrypt sensitive fields in database config"""
    if config:
        config['password'] = decrypt_credential(config.get('password', ''))
    return config


def load_db_configs():
    """Load database configurations from database"""
    conn = get_db_connection()
    rows = conn.fetchall('SELECT * FROM db_configs')

    return [_decrypt_db_config(dict(row)) for row in rows]


def get_db_config(config_id):
    """Get a single database config by ID"""
    conn = get_db_connection()
    row = conn.fetchone('SELECT * FROM db_configs WHERE id = ?', (config_id,))

    return _decrypt_db_config(dict(row)) if row else None


def create_db_config(config):
    """Create a new database config"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO db_configs (id, name, type, host, port, username, password, databases, docker_container, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (config['id'], config['name'], config.get('type', 'mysql'), config['host'], config.get('port', 3306),
          config['username'], encrypt_credential(config['password']), config.get('databases', '*'),
          config.get('docker_container') or None,
          config.get('status', 'active'), config.get('created_at', utc_isoformat()), config.get('updated_at')))
    conn.commit()



def update_db_config_in_db(config_id, config):
    """Update a database config"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE db_configs SET name=?, type=?, host=?, port=?, username=?, password=?, databases=?, docker_container=?, status=?, updated_at=?
        WHERE id=?
    ''', (config['name'], config.get('type', 'mysql'), config['host'], config.get('port', 3306),
          config['username'], encrypt_credential(config['password']), config.get('databases', '*'),
          config.get('docker_container') or None,
          config.get('status', 'active'), utc_isoformat(), config_id))
    conn.commit()



def delete_db_config_from_db(config_id):
    """Delete a database config"""
    conn = get_db_connection()
    conn.execute('DELETE FROM db_configs WHERE id = ?', (config_id,))
    conn.commit()



# --- Notification Channels ---

def load_notification_channels():
    """Load all notification channels from database"""
    conn = get_db_connection()
    rows = conn.fetchall('SELECT * FROM notification_channels')

    channels = []
    for row in rows:
        channel = dict(row)
        channel['config'] = json.loads(channel['config'] or '{}')
        channel['enabled'] = bool(channel['enabled'])
        channel['notify_on_success'] = bool(channel['notify_on_success'])
        channel['notify_on_failure'] = bool(channel['notify_on_failure'])
        channels.append(channel)
    return channels


def get_notification_channel(channel_id):
    """Get a single notification channel by ID"""
    conn = get_db_connection()
    row = conn.fetchone('SELECT * FROM notification_channels WHERE id = ?', (channel_id,))

    if row:
        channel = dict(row)
        channel['config'] = json.loads(channel['config'] or '{}')
        channel['enabled'] = bool(channel['enabled'])
        channel['notify_on_success'] = bool(channel['notify_on_success'])
        channel['notify_on_failure'] = bool(channel['notify_on_failure'])
        return channel
    return None


def create_notification_channel(channel):
    """Create a new notification channel"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO notification_channels (id, name, type, enabled, config, notify_on_success, notify_on_failure, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (channel['id'], channel['name'], channel['type'], bool(channel.get('enabled', True)),
          json.dumps(channel.get('config', {})), bool(channel.get('notify_on_success', True)),
          bool(channel.get('notify_on_failure', True)), channel.get('created_at', utc_isoformat()),
          channel.get('updated_at')))
    conn.commit()



def update_notification_channel(channel_id, channel):
    """Update a notification channel"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE notification_channels SET name=?, type=?, enabled=?, config=?, notify_on_success=?, notify_on_failure=?, updated_at=?
        WHERE id=?
    ''', (channel['name'], channel['type'], bool(channel.get('enabled', True)),
          json.dumps(channel.get('config', {})), bool(channel.get('notify_on_success', True)),
          bool(channel.get('notify_on_failure', True)), utc_isoformat(), channel_id))
    conn.commit()



def delete_notification_channel(channel_id):
    """Delete a notification channel"""
    conn = get_db_connection()
    conn.execute('DELETE FROM notification_channels WHERE id = ?', (channel_id,))
    conn.commit()



# --- Notification Senders ---

def format_duration(seconds):
    """Format duration in human-readable format"""
    if seconds < 60:
        return f"{int(seconds)}s"
    mins = int(seconds / 60)
    secs = int(seconds % 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours = int(mins / 60)
    remaining_mins = mins % 60
    return f"{hours}h {remaining_mins}m"


def get_status_emoji(status):
    """Get emoji for status"""
    emojis = {
        'success': '\u2705',  # Green checkmark
        'failed': '\u274C',   # Red X
        'timeout': '\u23F0',  # Alarm clock
        'error': '\u26A0\uFE0F'    # Warning sign
    }
    return emojis.get(status, '\u2753')  # Question mark for unknown


def send_email_notification(channel, job_name, status, message, duration):
    """Send notification via email"""
    config = channel['config']
    smtp_host = config.get('smtp_host', 'localhost')
    smtp_port = int(config.get('smtp_port', 587))
    smtp_user = config.get('smtp_user', '')
    smtp_password = config.get('smtp_password', '')
    smtp_tls = config.get('smtp_tls', True)
    from_address = config.get('from_address', smtp_user)
    to_addresses = config.get('to_addresses', [])

    if isinstance(to_addresses, str):
        to_addresses = [addr.strip() for addr in to_addresses.split(',') if addr.strip()]

    if not to_addresses:
        logger.warning("No email recipients configured")
        return

    # Create email
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"[BackupX] {job_name} - {status.upper()}"
    msg['From'] = from_address
    msg['To'] = ', '.join(to_addresses)

    # Plain text body
    text_body = f"""Backup Job: {job_name}
Status: {status.upper()}
Duration: {format_duration(duration)}
Time: {utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC

{message}
"""
    msg.attach(MIMEText(text_body, 'plain'))

    # Send email
    try:
        if smtp_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls(context=context)
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.sendmail(from_address, to_addresses, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.sendmail(from_address, to_addresses, msg.as_string())
        logger.info(f"Email notification sent for job {job_name}")
    except Exception as e:
        logger.error(f"Failed to send email notification: {e}")
        raise


def send_slack_notification(channel, job_name, status, message, duration):
    """Send notification to Slack via webhook"""
    config = channel['config']
    webhook_url = config.get('webhook_url', '')

    if not webhook_url:
        logger.warning("No Slack webhook URL configured")
        return

    emoji = get_status_emoji(status)
    color = '#36a64f' if status == 'success' else '#dc3545'

    payload = {
        "attachments": [{
            "color": color,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *Backup Job: {job_name}*\n*Status:* {status.upper()}\n*Duration:* {format_duration(duration)}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"```{message[:500]}```" if message else "_No additional details_"
                    }
                }
            ]
        }]
    }

    data = json.dumps(payload).encode('utf-8')
    req = Request(webhook_url, data=data, headers={'Content-Type': 'application/json'})

    try:
        with urlopen(req, timeout=30) as response:
            if response.status == 200:
                logger.info(f"Slack notification sent for job {job_name}")
            else:
                logger.warning(f"Slack webhook returned status {response.status}")
    except (URLError, HTTPError) as e:
        logger.error(f"Failed to send Slack notification: {e}")
        raise


def send_discord_notification(channel, job_name, status, message, duration):
    """Send notification to Discord via webhook"""
    config = channel['config']
    webhook_url = config.get('webhook_url', '')

    if not webhook_url:
        logger.warning("No Discord webhook URL configured")
        return

    emoji = get_status_emoji(status)
    color = 0x36a64f if status == 'success' else 0xdc3545

    payload = {
        "embeds": [{
            "title": f"{emoji} Backup Job: {job_name}",
            "color": color,
            "fields": [
                {"name": "Status", "value": status.upper(), "inline": True},
                {"name": "Duration", "value": format_duration(duration), "inline": True}
            ],
            "description": f"```{message[:1000]}```" if message else "_No additional details_",
            "timestamp": utc_isoformat()
        }]
    }

    data = json.dumps(payload).encode('utf-8')
    req = Request(webhook_url, data=data, headers={'Content-Type': 'application/json'})

    try:
        with urlopen(req, timeout=30) as response:
            if response.status in (200, 204):
                logger.info(f"Discord notification sent for job {job_name}")
            else:
                logger.warning(f"Discord webhook returned status {response.status}")
    except (URLError, HTTPError) as e:
        logger.error(f"Failed to send Discord notification: {e}")
        raise


def send_telegram_notification(channel, job_name, status, message, duration):
    """Send notification to Telegram via Bot API"""
    config = channel['config']
    bot_token = config.get('bot_token', '')
    chat_id = config.get('chat_id', '')

    if not bot_token or not chat_id:
        logger.warning("Telegram bot_token or chat_id not configured")
        return

    emoji = get_status_emoji(status)

    # Format message for Telegram (using HTML parse mode)
    text = f"""{emoji} <b>{job_name}</b> — {status.upper()}
Duration: {format_duration(duration)}
{message[:3000] if message else ''}"""

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    data = json.dumps(payload).encode('utf-8')
    req = Request(api_url, data=data, headers={'Content-Type': 'application/json'})

    try:
        with urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('ok'):
                logger.info(f"Telegram notification sent for job {job_name}")
            else:
                logger.warning(f"Telegram API error: {result.get('description', 'Unknown error')}")
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        logger.error(f"Failed to send Telegram notification: {e} — {body}")
        raise
    except URLError as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        raise


def send_webhook_notification(channel, job_name, status, message, duration):
    """Send notification to generic webhook"""
    config = channel['config']
    webhook_url = config.get('url', '')
    method = config.get('method', 'POST').upper()
    headers = config.get('headers', {})

    if not webhook_url:
        logger.warning("No webhook URL configured")
        return

    payload = {
        "job_name": job_name,
        "status": status,
        "message": message,
        "duration": duration,
        "timestamp": utc_isoformat()
    }

    data = json.dumps(payload).encode('utf-8')
    req_headers = {'Content-Type': 'application/json'}
    req_headers.update(headers)

    req = Request(webhook_url, data=data, headers=req_headers, method=method)

    try:
        with urlopen(req, timeout=30) as response:
            logger.info(f"Webhook notification sent for job {job_name} (status: {response.status})")
    except (URLError, HTTPError) as e:
        logger.error(f"Failed to send webhook notification: {e}")
        raise


def send_notification(job_id, job_name, status, message, duration):
    """Send notifications to all enabled channels"""
    channels = load_notification_channels()

    for channel in channels:
        if not channel['enabled']:
            continue

        # Check if should notify for this status
        if status == 'success' and not channel['notify_on_success']:
            continue
        if status != 'success' and not channel['notify_on_failure']:
            continue

        try:
            if channel['type'] == 'email':
                send_email_notification(channel, job_name, status, message, duration)
            elif channel['type'] == 'slack':
                send_slack_notification(channel, job_name, status, message, duration)
            elif channel['type'] == 'discord':
                send_discord_notification(channel, job_name, status, message, duration)
            elif channel['type'] == 'telegram':
                send_telegram_notification(channel, job_name, status, message, duration)
            elif channel['type'] == 'webhook':
                send_webhook_notification(channel, job_name, status, message, duration)
        except Exception as e:
            logger.error(f"Failed to send {channel['type']} notification to {channel['name']}: {e}")


def run_backup(job_id):
    """Execute a backup job"""
    job = get_job(job_id)
    if not job:
        return False, "Job not found"

    # Get S3 config to get skip_ssl_verify setting
    s3_config_id = job.get('s3_config_id')
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            job['skip_ssl_verify'] = s3_config.get('skip_ssl_verify', False)

    server_id = job.get('server_id')
    server = get_server(server_id) if server_id else None

    backup_type = job.get('backup_type', 'filesystem')

    # Ensure restic is installed on the remote server before running backup
    if server and server.get('ssh_user') and server.get('host'):
        ssh_key = _get_ssh_key_path(server)
        auth_type = server.get('ssh_auth_type', 'key_path')
        ssh_password = server.get('ssh_password', '')
        if ssh_password and is_encrypted(ssh_password):
            ssh_password = decrypt_credential(ssh_password)
        success, msg = ensure_restic_installed(
            server['host'], server['ssh_user'],
            int(server.get('ssh_port') or 22),
            ssh_key, ssh_auth_type=auth_type, ssh_password=ssh_password
        )
        if not success:
            error_msg = f"Restic not available on remote server: {msg}"
            update_job_status(job_id, 'error')
            add_history(job_id, job['name'], 'error', error_msg, 0)
            send_notification(job_id, job['name'], 'error', error_msg, 0)
            logger.error(f"Backup aborted - {error_msg}")
            return False, error_msg

    if backup_type == 'database':
        return run_database_backup(job_id, job)
    else:
        return run_filesystem_backup(job_id, job)


def sanitize_error_message(error: str, max_length: int = 500) -> str:
    """Sanitize error messages to prevent sensitive data leakage"""
    if not error:
        return "Unknown error"
    # Remove potential secrets from error messages
    sanitized = re.sub(r'(password|secret|key|token)[\s]*[=:]\s*[^\s]+', r'\1=***', error, flags=re.IGNORECASE)
    # Truncate to max length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + '...'
    return sanitized


def _write_server_key_file(server_id, key_content):
    """Write SSH key content to a per-server file and return the path"""
    key_dir = os.path.join(os.environ.get('APP_DATA_DIR', '/app/data'), 'ssh_keys')
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    key_path = os.path.join(key_dir, f'{server_id}.key')
    with open(key_path, 'w') as f:
        f.write(key_content)
        if not key_content.endswith('\n'):
            f.write('\n')
    os.chmod(key_path, 0o600)
    return key_path


def _get_ssh_key_path(server):
    """Get the effective SSH key path for a server, handling key_content if needed"""
    auth_type = server.get('ssh_auth_type', 'key_path')
    if auth_type == 'key_content':
        key_content = server.get('ssh_key_content', '')
        if key_content:
            # Decrypt if encrypted
            if is_encrypted(key_content):
                key_content = decrypt_credential(key_content)
            return _write_server_key_file(server.get('id', 'temp'), key_content)
    return server.get('ssh_key') or '/home/backupx/.ssh/id_rsa'


def _build_ssh_cmd(host, ssh_user, ssh_port=22, ssh_key='/home/backupx/.ssh/id_rsa', timeout=30, ssh_auth_type='key_path', ssh_password=None):
    """Build a standard SSH command prefix"""
    base_opts = [
        '-p', str(ssh_port),
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', f'ConnectTimeout={timeout}',
        '-o', 'ServerAliveInterval=60',
        '-o', 'ServerAliveCountMax=5',
    ]

    if ssh_auth_type == 'password':
        if not ssh_password:
            raise ValueError("Password authentication selected but no password provided")
        # Use sshpass for password authentication
        return [
            'sshpass', '-p', ssh_password,
            'ssh',
            *base_opts,
            '-o', 'PubkeyAuthentication=no',
            '-o', 'PreferredAuthentications=password',
            f'{ssh_user}@{host}'
        ]
    else:
        # Key-based authentication (key_path or key_content)
        return [
            'ssh', '-i', ssh_key,
            *base_opts,
            '-o', 'BatchMode=yes',
            f'{ssh_user}@{host}'
        ]


def _build_ssh_cmd_for_server(server, timeout=30):
    """Build SSH command from a server dict, handling all auth types"""
    auth_type = server.get('ssh_auth_type', 'key_path')
    ssh_key = _get_ssh_key_path(server)
    ssh_password = None
    if auth_type == 'password':
        ssh_password = server.get('ssh_password', '')
        if ssh_password and is_encrypted(ssh_password):
            ssh_password = decrypt_credential(ssh_password)
    return _build_ssh_cmd(
        server['host'], server.get('ssh_user', 'root'),
        int(server.get('ssh_port') or 22), ssh_key, timeout,
        ssh_auth_type=auth_type, ssh_password=ssh_password
    )


# Cache of servers where restic is confirmed installed (cleared on restart)
_restic_confirmed = set()

RESTIC_VERSION = '0.17.3'
RESTIC_INSTALL_SCRIPT = """
set -e
export PATH="$HOME/.local/bin:$PATH"
MIN_MAJOR=0
MIN_MINOR=14
if command -v restic >/dev/null 2>&1; then
    CURRENT=$(restic version 2>/dev/null | head -1 | awk '{print $2}')
    CUR_MAJOR=$(echo "$CURRENT" | cut -d. -f1)
    CUR_MINOR=$(echo "$CURRENT" | cut -d. -f2)
    if [ "$CUR_MAJOR" -gt "$MIN_MAJOR" ] || ([ "$CUR_MAJOR" -eq "$MIN_MAJOR" ] && [ "$CUR_MINOR" -ge "$MIN_MINOR" ]); then
        echo "RESTIC_ALREADY_INSTALLED"
        restic version
        exit 0
    fi
    echo "RESTIC_OUTDATED: $CURRENT - upgrading"
fi

ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
    armv7l)  ARCH="arm" ;;
    *)       echo "UNSUPPORTED_ARCH: $ARCH"; exit 1 ;;
esac

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
RESTIC_VER="%s"
URL="https://github.com/restic/restic/releases/download/v${RESTIC_VER}/restic_${RESTIC_VER}_${OS}_${ARCH}.bz2"

echo "RESTIC_INSTALLING from $URL"
TMPDIR=$(mktemp -d)
curl -fsSL "$URL" -o "$TMPDIR/restic.bz2"

# Decompress .bz2 using whatever is available
decompress_bz2() {
    if command -v bunzip2 >/dev/null 2>&1; then
        bunzip2 "$TMPDIR/restic.bz2" 2>/dev/null
    elif command -v bzip2 >/dev/null 2>&1; then
        bzip2 -d "$TMPDIR/restic.bz2" 2>/dev/null
    elif command -v bzcat >/dev/null 2>&1; then
        bzcat "$TMPDIR/restic.bz2" > "$TMPDIR/restic" 2>/dev/null && rm "$TMPDIR/restic.bz2"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import bz2,sys; open('$TMPDIR/restic','wb').write(bz2.decompress(open('$TMPDIR/restic.bz2','rb').read()))" 2>/dev/null && rm "$TMPDIR/restic.bz2"
    elif command -v python >/dev/null 2>&1; then
        python -c "import bz2,sys; open('$TMPDIR/restic','wb').write(bz2.decompress(open('$TMPDIR/restic.bz2','rb').read()))" 2>/dev/null && rm "$TMPDIR/restic.bz2"
    elif command -v perl >/dev/null 2>&1 && perl -MCompress::Bzip2 -e1 >/dev/null 2>&1; then
        perl -MCompress::Bzip2 -e 'my $b=Compress::Bzip2->new; open(I,"$ENV{TMPDIR}/restic.bz2"); open(O,">$ENV{TMPDIR}/restic"); while(read(I,my $buf,4096)){print O $b->decompress($buf)}' 2>/dev/null && rm "$TMPDIR/restic.bz2"
    else
        return 1
    fi
    [ -f "$TMPDIR/restic" ]
}

if ! decompress_bz2; then
    echo "Trying to install bzip2 via package manager..."
    if sudo -n true 2>/dev/null; then
        if command -v apt-get >/dev/null 2>&1; then
            sudo -n apt-get update -qq >/dev/null 2>&1
            sudo -n apt-get install -y bzip2 >/dev/null 2>&1
        elif command -v dnf >/dev/null 2>&1; then
            sudo -n dnf install -y bzip2 >/dev/null 2>&1
        elif command -v yum >/dev/null 2>&1; then
            sudo -n yum install -y bzip2 >/dev/null 2>&1
        elif command -v apk >/dev/null 2>&1; then
            sudo -n apk add --no-cache bzip2 >/dev/null 2>&1
        fi
    fi
    if ! decompress_bz2; then
        echo "FAILED_BZIP2: cannot decompress restic archive." >&2
        echo "Please install bzip2 on the server: apt-get install bzip2" >&2
        exit 1
    fi
fi
chmod +x "$TMPDIR/restic"

# Try /usr/local/bin with sudo, fall back to ~/.local/bin
if sudo -n mv "$TMPDIR/restic" /usr/local/bin/restic 2>/dev/null; then
    echo "RESTIC_INSTALLED_SYSTEM"
elif mv "$TMPDIR/restic" /usr/local/bin/restic 2>/dev/null; then
    echo "RESTIC_INSTALLED_SYSTEM"
else
    mkdir -p "$HOME/.local/bin"
    mv "$TMPDIR/restic" "$HOME/.local/bin/restic"
    # Ensure ~/.local/bin is in PATH for future sessions
    if ! grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    fi
    if ! grep -q '.local/bin' "$HOME/.profile" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
    fi
    echo "RESTIC_INSTALLED_USER"
fi
rmdir "$TMPDIR" 2>/dev/null || true
echo "RESTIC_INSTALLED"
restic version
""" % RESTIC_VERSION


# Cache of servers where DB clients are confirmed installed
_db_client_confirmed = {}  # cache_key -> set of installed client types ('mysql', 'postgres')

# Script to check for and install mysqldump / pg_dump using the system package manager
DB_CLIENT_INSTALL_SCRIPT = """
set -e
CLIENT_TYPE="%s"

check_binary() {
    case "$CLIENT_TYPE" in
        mysql)    command -v mysqldump >/dev/null 2>&1 ;;
        postgres) command -v pg_dump >/dev/null 2>&1 ;;
    esac
}

if check_binary; then
    echo "DB_CLIENT_ALREADY_INSTALLED"
    exit 0
fi

# Detect package manager and install
install_with() {
    PKG_MGR="$1"
    case "$CLIENT_TYPE:$PKG_MGR" in
        mysql:apt)     sudo -n apt-get update -qq && sudo -n apt-get install -y --no-install-recommends mariadb-client 2>/dev/null || sudo -n apt-get install -y --no-install-recommends default-mysql-client ;;
        mysql:dnf)     sudo -n dnf install -y mariadb 2>/dev/null || sudo -n dnf install -y mysql ;;
        mysql:yum)     sudo -n yum install -y mariadb 2>/dev/null || sudo -n yum install -y mysql ;;
        mysql:apk)     sudo -n apk add --no-cache mariadb-client 2>/dev/null || sudo -n apk add --no-cache mysql-client ;;
        postgres:apt)  sudo -n apt-get update -qq && sudo -n apt-get install -y --no-install-recommends postgresql-client ;;
        postgres:dnf)  sudo -n dnf install -y postgresql ;;
        postgres:yum)  sudo -n yum install -y postgresql ;;
        postgres:apk)  sudo -n apk add --no-cache postgresql-client ;;
        *) return 1 ;;
    esac
}

for pm in apt dnf yum apk; do
    if command -v "$pm" >/dev/null 2>&1 || command -v "$pm-get" >/dev/null 2>&1; then
        if [ "$pm" = "apt" ]; then pm_cmd="apt-get"; else pm_cmd="$pm"; fi
        if command -v "$pm_cmd" >/dev/null 2>&1; then
            echo "DB_CLIENT_INSTALLING with $pm"
            if install_with "$pm"; then
                if check_binary; then
                    echo "DB_CLIENT_INSTALLED"
                    exit 0
                fi
            fi
            echo "DB_CLIENT_INSTALL_FAILED with $pm"
            exit 1
        fi
    fi
done

echo "DB_CLIENT_NO_PACKAGE_MANAGER"
exit 1
"""


def ensure_db_client_installed(server, client_type):
    """Ensure mysqldump or pg_dump is installed on a remote server. Returns (success, message)."""
    if client_type not in ('mysql', 'postgres'):
        return False, f"Unknown client type: {client_type}"

    host = server.get('host', '')
    ssh_user = server.get('ssh_user', '')
    ssh_port = int(server.get('ssh_port') or 22)

    cache_key = f"{ssh_user}@{host}:{ssh_port}"
    if cache_key in _db_client_confirmed and client_type in _db_client_confirmed[cache_key]:
        return True, f"{client_type} client already confirmed"

    ssh_cmd = _build_ssh_cmd_for_server(server, timeout=30)
    script = DB_CLIENT_INSTALL_SCRIPT % client_type

    try:
        result = subprocess.run(
            ssh_cmd + [script],
            capture_output=True,
            text=True,
            timeout=180
        )

        output = result.stdout.strip()

        if result.returncode == 0:
            _db_client_confirmed.setdefault(cache_key, set()).add(client_type)
            if 'DB_CLIENT_ALREADY_INSTALLED' in output:
                logger.info(f"{client_type} client already installed on {cache_key}")
                return True, f"{client_type} client already installed"
            elif 'DB_CLIENT_INSTALLED' in output:
                logger.info(f"{client_type} client installed on {cache_key}")
                return True, f"{client_type} client installed"
            return True, output

        error = result.stderr.strip() or output
        if 'DB_CLIENT_NO_PACKAGE_MANAGER' in output:
            return False, f"No supported package manager found on {host}"
        logger.warning(f"Failed to install {client_type} client on {cache_key}: {error}")
        return False, f"Failed to install {client_type} client (may need sudo): {sanitize_error_message(error)}"

    except subprocess.TimeoutExpired:
        return False, f"{client_type} client installation timed out"
    except Exception as e:
        return False, f"Error installing {client_type} client: {sanitize_error_message(str(e))}"


def ensure_restic_installed(host, ssh_user, ssh_port=22, ssh_key='/home/backupx/.ssh/id_rsa', ssh_auth_type='key_path', ssh_password=None):
    """Check if restic is installed on remote server, install if missing. Returns (success, message)."""
    cache_key = f"{ssh_user}@{host}:{ssh_port}"
    if cache_key in _restic_confirmed:
        return True, "Restic already confirmed"

    ssh_cmd = _build_ssh_cmd(host, ssh_user, ssh_port, ssh_key, ssh_auth_type=ssh_auth_type, ssh_password=ssh_password)

    try:
        result = subprocess.run(
            ssh_cmd + [RESTIC_INSTALL_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120
        )

        output = result.stdout.strip()

        if result.returncode == 0:
            _restic_confirmed.add(cache_key)
            if 'RESTIC_ALREADY_INSTALLED' in output:
                logger.info(f"Restic already installed on {cache_key}")
                return True, "Restic already installed"
            elif 'RESTIC_INSTALLED' in output:
                logger.info(f"Restic installed successfully on {cache_key}")
                return True, "Restic installed successfully"
            return True, output

        error = result.stderr.strip() or output
        if 'UNSUPPORTED_ARCH' in output:
            return False, f"Unsupported architecture on {host}: {output}"
        logger.error(f"Failed to install restic on {cache_key}: {error}")
        return False, f"Failed to install restic: {sanitize_error_message(error)}"

    except subprocess.TimeoutExpired:
        return False, "Restic installation timed out"
    except Exception as e:
        return False, f"Error checking restic: {sanitize_error_message(str(e))}"


def _format_bytes(num_bytes):
    """Format bytes into human-readable size"""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def _stream_restic_progress(proc, job_id, timeout):
    """Stream restic --json output, parse progress, update job progress in DB.

    Returns (returncode, stderr_text).
    """
    import time

    stderr_lines = []
    deadline = time.monotonic() + timeout

    for line in proc.stdout:
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(cmd='restic', timeout=timeout)

        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON output (e.g. "Initializing new restic repository...")
            continue

        msg_type = msg.get('message_type')

        if msg_type == 'status':
            pct = msg.get('percent_done', 0)
            total = msg.get('total_bytes', 0)
            done = msg.get('bytes_done', 0)
            # Map restic 0-100% into our 30-90% progress range
            progress = 30 + int(pct * 60)
            if total > 0:
                status_msg = f"Backing up: {_format_bytes(done)} / {_format_bytes(total)} ({pct:.0%})"
            else:
                status_msg = f"Backing up: {_format_bytes(done)} ({pct:.0%})"
            update_job_progress(job_id, progress, status_msg)

        elif msg_type == 'summary':
            total = msg.get('total_bytes_processed', 0)
            added = msg.get('data_added', 0)
            files_new = msg.get('files_new', 0)
            files_changed = msg.get('files_changed', 0)
            duration = msg.get('total_duration', 0)
            update_job_progress(job_id, 90,
                f"Done: {_format_bytes(total)} processed, {_format_bytes(added)} added, "
                f"{files_new} new / {files_changed} changed files in {duration:.1f}s")

    # Collect stderr
    if proc.stderr:
        stderr_lines = proc.stderr.readlines()

    proc.wait()
    return proc.returncode, ''.join(stderr_lines)


def run_filesystem_backup(job_id, job):
    """Execute a filesystem backup job"""
    start_time = utc_now()
    logger.info(f"Starting filesystem backup job: {job_id} ({job['name']})")

    # Update job status
    update_job_status(job_id, 'running', last_run=start_time.isoformat())
    update_job_progress(job_id, 0, 'Initializing backup...')

    # Look up server for SSH auth details
    server = get_server(job['server_id']) if job.get('server_id') else None

    try:
        # Build exclude args with proper escaping
        update_job_progress(job_id, 10, 'Preparing backup configuration...')
        exclude_args = []
        for pattern in job.get('excludes', []):
            exclude_args.append(f'--exclude {shlex.quote(pattern)}')

        # Build directory list with proper escaping
        directories = ' '.join(shlex.quote(d) for d in job['directories'])

        # Escape all values for shell
        s3_access_key = shlex.quote(job['s3_access_key'])
        s3_secret_key = shlex.quote(job['s3_secret_key'])
        restic_password = shlex.quote(job['restic_password'])
        s3_endpoint = shlex.quote(job['s3_endpoint'])
        s3_bucket = shlex.quote(job['s3_bucket'])
        backup_prefix = shlex.quote(job['backup_prefix'])

        # Run backup via SSH on remote
        if server:
            ssh_cmd = _build_ssh_cmd_for_server(server, timeout=30)
        else:
            ssh_cmd = _build_ssh_cmd(
                job['remote_host'].split('@')[-1] if '@' in job.get('remote_host', '') else job.get('remote_host', ''),
                job['remote_host'].split('@')[0] if '@' in job.get('remote_host', '') else 'root',
                int(job.get('ssh_port', 22)),
                job.get('ssh_key', '/home/backupx/.ssh/id_rsa'))

        # Build remote command with proper escaping
        insecure_flag = '--insecure-tls' if job.get('skip_ssl_verify') else ''
        remote_cmd = f"""
export PATH="$HOME/.local/bin:$PATH"
export AWS_ACCESS_KEY_ID={s3_access_key}
export AWS_SECRET_ACCESS_KEY={s3_secret_key}
export RESTIC_PASSWORD={restic_password}
export RESTIC_REPOSITORY="s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

# Ensure repository exists (init if needed)
if ! restic cat config {insecure_flag} >/dev/null 2>&1; then
    echo "Initializing new restic repository..."
    restic init {insecure_flag} 2>&1 || true
fi

restic backup --json --compression auto --tag automated {insecure_flag} {' '.join(exclude_args)} {directories}
"""

        # Execute with streaming progress
        update_job_progress(job_id, 20, 'Connecting to remote server...')
        update_job_progress(job_id, 30, 'Starting backup on remote server...')
        backup_timeout = job.get('timeout', 28800)  # 8 hour default timeout
        proc = subprocess.Popen(
            ssh_cmd + [remote_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            returncode, stderr_text = _stream_restic_progress(proc, job_id, backup_timeout)
        except subprocess.TimeoutExpired:
            raise

        duration = (utc_now() - start_time).total_seconds()

        if returncode == 0:
            update_job_progress(job_id, 100, 'Backup completed successfully')
            invalidate_snapshot_cache(job_id)
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=utc_isoformat())
            add_history(job_id, job['name'], 'success', 'Backup completed successfully', duration)
            send_notification(job_id, job['name'], 'success', 'Backup completed successfully', duration)
            logger.info(f"Filesystem backup completed successfully: {job_id} (duration: {duration:.1f}s)")
            return True, "Backup completed successfully"
        else:
            error_msg = sanitize_error_message(stderr_text)
            update_job_status(job_id, 'failed')
            add_history(job_id, job['name'], 'failed', error_msg, duration)
            send_notification(job_id, job['name'], 'failed', error_msg, duration)
            logger.error(f"Filesystem backup failed: {job_id} - {error_msg[:200]}")
            return False, error_msg

    except subprocess.TimeoutExpired:
        update_job_status(job_id, 'timeout')
        add_history(job_id, job['name'], 'timeout', 'Backup timed out', 0)
        send_notification(job_id, job['name'], 'timeout', 'Backup timed out', 0)
        logger.error(f"Filesystem backup timed out: {job_id}")
        return False, "Backup timed out"
    except Exception as e:
        error_msg = sanitize_error_message(str(e))
        update_job_status(job_id, 'error')
        add_history(job_id, job['name'], 'error', error_msg, 0)
        send_notification(job_id, job['name'], 'error', error_msg, 0)
        logger.exception(f"Filesystem backup error: {job_id}")
        return False, error_msg


def run_database_backup(job_id, job):
    """Execute a MySQL database backup job"""
    start_time = utc_now()
    logger.info(f"Starting database backup job: {job_id} ({job['name']})")

    # Update job status
    update_job_status(job_id, 'running', last_run=start_time.isoformat())
    update_job_progress(job_id, 0, 'Initializing database backup...')

    # Look up server for SSH auth details
    server = get_server(job['server_id']) if job.get('server_id') else None

    try:
        # Get database config
        update_job_progress(job_id, 10, 'Loading database configuration...')
        db_config_id = job.get('database_config_id')
        if not db_config_id:
            raise Exception("Database configuration not specified")

        db_configs = load_db_configs()
        db_config = next((c for c in db_configs if c['id'] == db_config_id), None)
        if not db_config:
            raise Exception("Database configuration not found")

        # Auto-install database client if missing (skipped when using docker exec — client is inside the container)
        if server and not (db_config.get('docker_container') or '').strip():
            db_type = db_config.get('type', 'mysql')
            client_type = 'postgres' if db_type in ('postgres', 'postgresql') else 'mysql'
            update_job_progress(job_id, 15, f'Checking {client_type} client on remote server...')
            ok, msg = ensure_db_client_installed(server, client_type)
            if not ok:
                raise Exception(f"{client_type} client not available on remote server: {msg}")

        # Build SSH command to run on remote server
        if server:
            ssh_cmd = _build_ssh_cmd_for_server(server, timeout=30)
        else:
            ssh_cmd = _build_ssh_cmd(
                job['remote_host'].split('@')[-1] if '@' in job.get('remote_host', '') else job.get('remote_host', ''),
                job['remote_host'].split('@')[0] if '@' in job.get('remote_host', '') else 'root',
                int(job.get('ssh_port', 22)),
                job.get('ssh_key', '/home/backupx/.ssh/id_rsa'))

        # Get database list with proper escaping
        databases = db_config.get('databases', '*')
        if databases == '*':
            db_flag = '--all-databases'
        else:
            # Multiple databases separated by comma or single db - escape each
            db_list = [shlex.quote(db.strip()) for db in databases.split(',') if db.strip()]
            db_flag = '--databases ' + ' '.join(db_list)

        # Check if we should use docker exec instead of direct connection
        container = db_config.get('docker_container') or ''
        use_docker = bool(container.strip())

        # Determine db type: for docker-exec mode we auto-detect at run time;
        # for direct connection we use the explicit type from the config
        db_type = db_config.get('type', 'mysql')
        is_postgres = db_type in ('postgres', 'postgresql')

        # Generate backup filename with timestamp
        timestamp = utc_now().strftime('%Y%m%d_%H%M%S')
        prefix = 'db' if use_docker else ('pg' if is_postgres else 'mysql')
        backup_filename = f"{prefix}_backup_{timestamp}.sql.gz"

        # Escape all values for shell
        s3_access_key = shlex.quote(job['s3_access_key'])
        s3_secret_key = shlex.quote(job['s3_secret_key'])
        restic_password = shlex.quote(job['restic_password'])
        db_host = shlex.quote(db_config['host'] or '')
        default_port = 5432 if is_postgres else 3306
        db_port = int(db_config.get('port', default_port))
        db_user = shlex.quote(db_config['username'] or '')
        db_pass = shlex.quote(db_config['password'] or '')
        safe_container = shlex.quote(container) if use_docker else ''

        # Build the dump command
        if use_docker:
            # Use the db_type from the config directly - it was set when the user picked
            # the container (either manually or via "Pick from server")
            if is_postgres:
                dump_inner = f'pg_dumpall -U postgres'
                backup_tag = 'postgres-backup'
                dump_error_msg = 'pg_dumpall failed'
            else:
                dump_inner = f'mysqldump --all-databases --single-transaction --routines --triggers'
                backup_tag = 'mysql-backup'
                dump_error_msg = 'mysqldump failed'

            dump_cmd = f'''sh -c '
if docker ps >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo -n docker"; fi
$DOCKER exec {safe_container} {dump_inner}
' '''
        elif is_postgres:
            # Direct connection from jump server
            if databases == '*':
                dump_cmd = f'PGPASSWORD={db_pass} pg_dumpall -h {db_host} -p {db_port} -U {db_user}'
            else:
                db_list_raw = [db.strip() for db in databases.split(',') if db.strip()]
                if len(db_list_raw) == 1:
                    dump_cmd = f'PGPASSWORD={db_pass} pg_dump -h {db_host} -p {db_port} -U {db_user} {shlex.quote(db_list_raw[0])}'
                else:
                    dump_parts = [f'PGPASSWORD={db_pass} pg_dump -h {db_host} -p {db_port} -U {db_user} {shlex.quote(db)}' for db in db_list_raw]
                    dump_cmd = ' && echo "-- NEXT DB --" && '.join(dump_parts)
            backup_tag = 'postgres-backup'
            dump_error_msg = 'pg_dump failed'
        else:
            dump_cmd = f'mysqldump -h {db_host} -P {db_port} -u {db_user} -p{db_pass} {db_flag} --single-transaction --routines --triggers'
            backup_tag = 'mysql-backup'
            dump_error_msg = 'mysqldump failed'

        # Build remote command with proper escaping
        insecure_flag = '--insecure-tls' if job.get('skip_ssl_verify') else ''
        remote_cmd = f"""
export PATH="$HOME/.local/bin:$PATH"
export AWS_ACCESS_KEY_ID={s3_access_key}
export AWS_SECRET_ACCESS_KEY={s3_secret_key}
export RESTIC_PASSWORD={restic_password}
export RESTIC_REPOSITORY="s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

# Ensure repository exists (init if needed, ignore "already initialized" errors)
if ! restic cat config {insecure_flag} >/dev/null 2>&1; then
    echo "Initializing new restic repository..."
    restic init {insecure_flag} 2>&1 || true
fi

# Create temp directory for backup
BACKUP_DIR=$(mktemp -d)
BACKUP_FILE="$BACKUP_DIR/{backup_filename}"

# Dump database
( {dump_cmd} ) | gzip > "$BACKUP_FILE"

if [ $? -ne 0 ]; then
    echo "{dump_error_msg}"
    rm -rf "$BACKUP_DIR"
    exit 1
fi

# Backup to restic repository
restic backup --json --compression auto --tag automated --tag {backup_tag} {insecure_flag} "$BACKUP_FILE"
RESTIC_EXIT=$?

# Cleanup
rm -rf "$BACKUP_DIR"

exit $RESTIC_EXIT
"""

        # Execute with streaming progress
        update_job_progress(job_id, 20, 'Connecting to remote server...')
        update_job_progress(job_id, 30, 'Dumping database...')
        backup_timeout = job.get('timeout', 28800)  # 8 hour default timeout
        proc = subprocess.Popen(
            ssh_cmd + [remote_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            returncode, stderr_text = _stream_restic_progress(proc, job_id, backup_timeout)
        except subprocess.TimeoutExpired:
            raise

        duration = (utc_now() - start_time).total_seconds()

        if returncode == 0:
            update_job_progress(job_id, 100, 'Database backup completed successfully')
            invalidate_snapshot_cache(job_id)
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=utc_isoformat())
            message = f'Database backup completed successfully ({databases})'
            add_history(job_id, job['name'], 'success', message, duration)
            send_notification(job_id, job['name'], 'success', message, duration)
            logger.info(f"Database backup completed successfully: {job_id} (duration: {duration:.1f}s)")
            return True, "Database backup completed successfully"
        else:
            error_msg = sanitize_error_message(stderr_text)
            update_job_status(job_id, 'failed')
            add_history(job_id, job['name'], 'failed', error_msg, duration)
            send_notification(job_id, job['name'], 'failed', error_msg, duration)
            logger.error(f"Database backup failed: {job_id} - {error_msg[:200]}")
            return False, error_msg

    except subprocess.TimeoutExpired:
        update_job_status(job_id, 'timeout')
        add_history(job_id, job['name'], 'timeout', 'Database backup timed out', 0)
        send_notification(job_id, job['name'], 'timeout', 'Database backup timed out', 0)
        logger.error(f"Database backup timed out: {job_id}")
        return False, "Database backup timed out"
    except Exception as e:
        error_msg = sanitize_error_message(str(e))
        update_job_status(job_id, 'error')
        add_history(job_id, job['name'], 'error', error_msg, 0)
        send_notification(job_id, job['name'], 'error', error_msg, 0)
        logger.exception(f"Database backup error: {job_id}")
        return False, error_msg


# Simple in-memory cache for snapshots and stats (TTL in seconds)
_snapshot_cache = {}
_snapshot_cache_ttl = 60  # 1 minute
_stats_cache = {}
_stats_cache_ttl = 300  # 5 minutes


def invalidate_snapshot_cache(job_id):
    """Invalidate cached snapshots/stats for a job (call after backup/restore)"""
    _snapshot_cache.pop(job_id, None)
    _stats_cache.pop(job_id, None)


def get_snapshots(job, server=None):
    """Get list of snapshots for a job"""
    import time
    job_id = job.get('id')
    # Check cache
    if job_id and job_id in _snapshot_cache:
        cached_at, cached_data = _snapshot_cache[job_id]
        if time.time() - cached_at < _snapshot_cache_ttl:
            return cached_data

    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    # Run restic command to list snapshots
    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        cmd = ['restic', 'snapshots', '--json']
        if skip_ssl_verify:
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        if result.returncode == 0:
            snapshots = json.loads(result.stdout)
            # Sort by time, newest first
            snapshots.sort(key=lambda x: x.get('time', ''), reverse=True)
            # Cache result
            if job_id:
                _snapshot_cache[job_id] = (time.time(), snapshots)
            return snapshots
        return []
    except Exception as e:
        logger.error(f"Failed to get snapshots: {e}")
        return []


def get_repo_stats(job, server=None):
    import time
    job_id = job.get('id')
    # Check cache
    if job_id and job_id in _stats_cache:
        cached_at, cached_data = _stats_cache[job_id]
        if time.time() - cached_at < _stats_cache_ttl:
            return cached_data
    """Get repository statistics"""
    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    # Run restic command to get stats
    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        cmd = ['restic', 'stats', '--json', '--mode', 'raw-data']
        if skip_ssl_verify:
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        if result.returncode == 0:
            stats = json.loads(result.stdout)
            if job_id:
                _stats_cache[job_id] = (time.time(), stats)
            return stats
        return None
    except Exception as e:
        logger.error(f"Failed to get repo stats: {e}")
        return None


def schedule_job(job_id, job):
    """Schedule a backup job"""
    # Remove existing job if any
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass  # Job may not exist, which is fine

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
@limiter.exempt
def health():
    return jsonify({'status': 'ok'})


# API Authentication Routes
@app.route('/api/auth/login', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute")  # Strict rate limiting for login to prevent brute-force
def api_login():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    username = data.get('username')
    password = data.get('password')

    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    ip_address = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')

    # Use secure password hash comparison
    if username == admin_user and check_password_hash(get_admin_password_hash(), password):
        user = User(username)
        login_user(user)
        logger.info(f"Successful login for user: {username}")
        # Log successful login
        try:
            from .audit.decorator import audit_login
            audit_login(username, username, True, ip_address, user_agent)
        except Exception as e:
            logger.debug(f"Audit logging failed: {e}")
        return jsonify({'user': {'id': username, 'username': username}})

    logger.warning(f"Failed login attempt for user: {username} from {request.remote_addr}")
    # Log failed login
    try:
        from .audit.decorator import audit_login
        audit_login(username or 'unknown', username or 'unknown', False, ip_address, user_agent)
    except Exception as e:
        logger.debug(f"Audit logging failed: {e}")
    return jsonify({'error': 'Invalid credentials'}), 401


@app.route('/api/auth/logout', methods=['POST'])
@csrf.exempt
def api_logout():
    # Log logout before actually logging out
    if current_user.is_authenticated:
        try:
            from .audit.decorator import audit_logout
            audit_logout(
                current_user.id,
                current_user.id,
                request.remote_addr,
                request.headers.get('User-Agent', '')
            )
        except Exception as e:
            logger.debug(f"Audit logging failed: {e}")
    logout_user()
    return jsonify({'success': True})


@app.route('/api/auth/me')
def api_me():
    if current_user.is_authenticated:
        return jsonify({'id': current_user.id, 'username': current_user.id})
    return jsonify({'error': 'Not authenticated'}), 401


@app.route('/api/auth/change-password', methods=['POST'])
@login_required
@csrf.exempt
def api_change_password():
    """Change the admin password"""
    global _admin_password_hash, _admin_password_hash_loaded

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    current_password = data.get('current_password', '').strip()
    new_password = data.get('new_password', '').strip()

    if not current_password or not new_password:
        return jsonify({'error': 'Current password and new password are required'}), 400

    # Validate new password strength
    if len(new_password) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400

    # In production, require stronger passwords
    if is_production_mode() and len(new_password) < 12:
        return jsonify({'error': 'Password must be at least 12 characters in production'}), 400

    # Verify current password
    if not check_password_hash(get_admin_password_hash(), current_password):
        # Log failed attempt
        logger.warning(f"Failed password change attempt for user: {current_user.id} from {request.remote_addr}")
        return jsonify({'error': 'Current password is incorrect'}), 401

    # Generate new password hash
    new_hash = generate_password_hash(new_password)

    # Store new password hash in database settings (primary storage)
    success, error = set_app_setting('admin_password_hash', new_hash)
    if not success:
        logger.error(f"Failed to save password to database: {error}")
        return jsonify({'error': 'Failed to save password. Please try again.'}), 500

    # Update the in-memory cache
    _admin_password_hash = new_hash
    _admin_password_hash_loaded = True

    # Audit log the password change
    try:
        from .audit.logger import get_audit_logger
        audit = get_audit_logger()
        if audit:
            audit.log(
                action='UPDATE',
                resource_type='user',
                resource_id=current_user.id,
                resource_name=current_user.id,
                user_id=current_user.id,
                user_name=current_user.id,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent', ''),
                new_value={'action': 'password_changed'}
            )
    except Exception as e:
        logger.debug(f"Audit logging failed: {e}")

    logger.info(f"Password changed for user: {current_user.id}")
    return jsonify({'success': True, 'message': 'Password changed successfully'})


# API Routes
@app.route('/api/jobs')
@login_required
@limiter.exempt
def api_jobs():
    jobs = load_jobs()
    return jsonify(jobs)


@app.route('/api/jobs/<job_id>/reveal-password', methods=['POST'])
@login_required
@csrf.exempt
def api_reveal_restic_password(job_id):
    """Reveal the restic password for a job. Audited."""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    password = job.get('restic_password', '')
    if not password:
        return jsonify({'error': 'No password stored'}), 404

    # Audit log this action
    try:
        from .audit.logger import get_audit_logger
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log(
                action='reveal',
                resource_type='job',
                resource_id=job_id,
                resource_name=job.get('name'),
                user_id=current_user.id if hasattr(current_user, 'id') else 'unknown',
                user_name=current_user.id if hasattr(current_user, 'id') else 'unknown',
                status='success',
                new_value={'field': 'restic_password'},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent', '')
            )
    except Exception as e:
        logger.warning(f"Failed to audit reveal: {e}")

    return jsonify({'restic_password': password})


@app.route('/api/jobs/<job_id>/status')
@login_required
@limiter.exempt
def api_job_status(job_id):
    job = get_job(job_id)
    if job:
        return jsonify({
            'status': job.get('status', 'unknown'),
            'last_run': job.get('last_run'),
            'last_success': job.get('last_success')
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

    # Validate job_id format (alphanumeric, hyphens, underscores only)
    if not re.match(r'^[a-z0-9][a-z0-9\-_]{0,62}[a-z0-9]$', job_id) and len(job_id) > 1:
        return jsonify({'error': 'Job ID must be 2-64 alphanumeric characters, hyphens, or underscores'}), 400

    # Check if job already exists
    if get_job(job_id):
        return jsonify({'error': 'Job ID already exists'}), 400

    # Validate schedule cron if provided
    schedule_cron = data.get('schedule_cron', '0 2 * * *')
    if data.get('schedule_enabled') and not validate_cron(schedule_cron):
        return jsonify({'error': 'Invalid cron expression format'}), 400

    # Resolve server and S3 config
    server_id = data.get('server_id')
    s3_config_id = data.get('s3_config_id')

    server = None
    s3_config = None

    if server_id:
        server = get_server(server_id)
        if not server:
            return jsonify({'error': 'Server not found'}), 400

    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if not s3_config:
            return jsonify({'error': 'S3 configuration not found'}), 400

    # Get backup type
    backup_type = data.get('backup_type', 'filesystem')

    job = {
        'name': data.get('name', job_id),
        'backup_type': backup_type,
        'server_id': server_id,
        's3_config_id': s3_config_id,
        # Store resolved values for backup execution
        'remote_host': f"{server['ssh_user']}@{server['host']}" if server else data.get('remote_host'),
        'ssh_port': (server.get('ssh_port') or 22) if server else int(data.get('ssh_port', 22)),
        'ssh_key': (server.get('ssh_key') or '/home/backupx/.ssh/id_rsa') if server else data.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
        's3_endpoint': s3_config['endpoint'] if s3_config else data.get('s3_endpoint'),
        's3_bucket': s3_config['bucket'] if s3_config else data.get('s3_bucket'),
        's3_access_key': s3_config['access_key'] if s3_config else data.get('s3_access_key'),
        's3_secret_key': s3_config['secret_key'] if s3_config else data.get('s3_secret_key'),
        'skip_ssl_verify': s3_config.get('skip_ssl_verify', False) if s3_config else data.get('skip_ssl_verify', False),
        # Filesystem backup fields
        'directories': data.get('directories', []),
        'excludes': data.get('excludes', []),
        # Database backup fields
        'database_config_id': data.get('database_config_id'),
        # Common fields
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
        'created_at': utc_isoformat()
    }

    save_job(job_id, job)

    if job['schedule_enabled']:
        schedule_job(job_id, job)

    # Audit log
    try:
        from .audit.logger import get_audit_logger, AuditLogger
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log(
                action=AuditLogger.ACTION_CREATE,
                resource_type=AuditLogger.RESOURCE_JOB,
                resource_id=job_id,
                resource_name=job.get('name', job_id),
                new_value=job,
                user_id=current_user.id if current_user.is_authenticated else None,
                user_name=current_user.id if current_user.is_authenticated else None,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent', '')
            )
    except Exception as e:
        logger.debug(f"Audit logging failed: {e}")

    return jsonify({'success': True, 'job_id': job_id}), 201


@app.route('/api/jobs/<job_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_job(job_id):
    """Update an existing backup job"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    # Resolve server and S3 config if provided
    server_id = data.get('server_id', job.get('server_id'))
    s3_config_id = data.get('s3_config_id', job.get('s3_config_id'))

    server = None
    s3_config = None

    if server_id:
        server = get_server(server_id)
        if not server:
            return jsonify({'error': 'Server not found'}), 400

    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if not s3_config:
            return jsonify({'error': 'S3 configuration not found'}), 400

    job.update({
        'name': data.get('name', job['name']),
        'backup_type': data.get('backup_type', job.get('backup_type', 'filesystem')),
        'server_id': server_id,
        's3_config_id': s3_config_id,
        # Store resolved values for backup execution
        'remote_host': f"{server['ssh_user']}@{server['host']}" if server else job.get('remote_host'),
        'ssh_port': (server.get('ssh_port') or 22) if server else job.get('ssh_port', 22),
        'ssh_key': (server.get('ssh_key') or '/home/backupx/.ssh/id_rsa') if server else job.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
        's3_endpoint': s3_config['endpoint'] if s3_config else job.get('s3_endpoint'),
        's3_bucket': s3_config['bucket'] if s3_config else job.get('s3_bucket'),
        's3_access_key': s3_config['access_key'] if s3_config else job.get('s3_access_key'),
        'skip_ssl_verify': s3_config.get('skip_ssl_verify', False) if s3_config else job.get('skip_ssl_verify', False),
        # Filesystem backup fields
        'directories': data.get('directories', job.get('directories', [])),
        'excludes': data.get('excludes', job.get('excludes', [])),
        # Database backup fields
        'database_config_id': data.get('database_config_id', job.get('database_config_id')),
        # Common fields
        'backup_prefix': data.get('backup_prefix', job.get('backup_prefix', job_id)),
        'schedule_enabled': data.get('schedule_enabled', job.get('schedule_enabled', False)),
        'schedule_cron': data.get('schedule_cron', job.get('schedule_cron', '0 2 * * *')),
        'retention_hourly': int(data.get('retention_hourly', job.get('retention_hourly', 24))),
        'retention_daily': int(data.get('retention_daily', job.get('retention_daily', 7))),
        'retention_weekly': int(data.get('retention_weekly', job.get('retention_weekly', 4))),
        'retention_monthly': int(data.get('retention_monthly', job.get('retention_monthly', 12))),
        'timeout': int(data.get('timeout', job.get('timeout', 7200))),
        'updated_at': utc_isoformat()
    })

    # Only update secrets if provided
    if s3_config:
        job['s3_secret_key'] = s3_config['secret_key']
    elif data.get('s3_secret_key'):
        job['s3_secret_key'] = data['s3_secret_key']
    if data.get('restic_password'):
        job['restic_password'] = data['restic_password']

    save_job(job_id, job)
    schedule_job(job_id, job)

    # Audit log
    try:
        from .audit.logger import get_audit_logger, AuditLogger
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log(
                action=AuditLogger.ACTION_UPDATE,
                resource_type=AuditLogger.RESOURCE_JOB,
                resource_id=job_id,
                resource_name=job.get('name', job_id),
                new_value=job,
                user_id=current_user.id if current_user.is_authenticated else None,
                user_name=current_user.id if current_user.is_authenticated else None,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent', '')
            )
    except Exception as e:
        logger.debug(f"Audit logging failed: {e}")

    return jsonify({'success': True})


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_job(job_id):
    """Delete a backup job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    job_name = job.get('name', job_id)
    delete_job_from_db(job_id)

    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass  # Job may not be scheduled

    # Audit log
    try:
        from .audit.logger import get_audit_logger, AuditLogger
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log(
                action=AuditLogger.ACTION_DELETE,
                resource_type=AuditLogger.RESOURCE_JOB,
                resource_id=job_id,
                resource_name=job_name,
                old_value=job,
                user_id=current_user.id if current_user.is_authenticated else None,
                user_name=current_user.id if current_user.is_authenticated else None,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent', '')
            )
    except Exception as e:
        logger.debug(f"Audit logging failed: {e}")

    return jsonify({'success': True})


@app.route('/api/jobs/<job_id>/run', methods=['POST'])
@login_required
@csrf.exempt
def api_run_job(job_id):
    """Run a backup job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'}), 404

    # Audit log
    try:
        from .audit.decorator import audit_backup_run
        audit_backup_run(
            job_id,
            job.get('name', job_id),
            user_id=current_user.id if current_user.is_authenticated else None,
            user_name=current_user.id if current_user.is_authenticated else None,
            ip_address=request.remote_addr,
            triggered_by='manual'
        )
    except Exception as e:
        logger.debug(f"Audit logging failed: {e}")

    # Run in background (non-daemon so it survives worker shutdowns if possible)
    thread = threading.Thread(target=run_backup, args=[job_id], daemon=False)
    thread.start()

    return jsonify({'success': True, 'message': 'Backup started'})


@app.route('/api/jobs/<job_id>/init', methods=['POST'])
@login_required
@csrf.exempt
def api_init_job_repo(job_id):
    """Initialize restic repository for a job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'}), 404

    # Get S3 config to get skip_ssl_verify setting
    s3_config_id = job.get('s3_config_id')
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            job['skip_ssl_verify'] = s3_config.get('skip_ssl_verify', False)

    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job.get('backup_prefix', job_id)}"

        cmd = ['restic', 'init']
        if job.get('skip_ssl_verify'):
            cmd.append('--insecure-tls')

        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)

        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'Repository initialized'})
        elif 'already initialized' in result.stderr.lower() or 'already exists' in result.stderr.lower():
            return jsonify({'success': True, 'message': 'Repository already initialized'})
        else:
            return jsonify({'success': False, 'error': result.stderr or 'Failed to initialize repository'}), 400

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Initialization timed out'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jobs/<job_id>/snapshots')
@login_required
@limiter.exempt
def api_job_snapshots(job_id):
    """Get snapshots for a job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    # Get server details
    server_id = job.get('server_id')
    server = get_server(server_id) if server_id else None

    # Only fetch snapshots (fast). Stats is fetched separately on-demand.
    snapshots = get_snapshots(job, server)

    return jsonify({
        'snapshots': snapshots,
        'stats': None
    })


@app.route('/api/jobs/<job_id>/snapshots/stats')
@login_required
def api_job_snapshot_stats(job_id):
    """Get repository statistics (slow - fetched separately)"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    server_id = job.get('server_id')
    server = get_server(server_id) if server_id else None

    stats = get_repo_stats(job, server)
    return jsonify({'stats': stats})


@app.route('/api/jobs/<job_id>/snapshots/<snapshot_id>/files')
@login_required
def api_snapshot_files(job_id, snapshot_id):
    """List files in a snapshot"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    path = request.args.get('path', '/')

    # Sanitize path - prevent path traversal attacks
    normalized_path = os.path.normpath(path)
    if '..' in normalized_path or normalized_path.startswith('/etc') or normalized_path.startswith('/root'):
        return jsonify({'error': 'Invalid path'}), 400

    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        # Use restic ls to list files in the snapshot at the given path
        cmd = ['restic', 'ls', '--json', snapshot_id, path]
        if skip_ssl_verify:
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        if result.returncode != 0:
            return jsonify({'error': result.stderr or 'Failed to list files'}), 400

        # Parse JSON lines output from restic ls
        files = []
        seen_paths = set()
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                item = json.loads(line)
                item_path = item.get('path', '')

                # Filter to only direct children of the requested path
                if path == '/':
                    # Root level - get top-level items
                    rel_path = item_path.lstrip('/')
                    if '/' in rel_path:
                        # This is nested, extract the top-level directory
                        top_dir = rel_path.split('/')[0]
                        if top_dir and top_dir not in seen_paths:
                            seen_paths.add(top_dir)
                            files.append({
                                'name': top_dir,
                                'path': '/' + top_dir,
                                'type': 'dir',
                                'size': 0,
                                'mtime': ''
                            })
                    elif rel_path and rel_path not in seen_paths:
                        seen_paths.add(rel_path)
                        files.append({
                            'name': item.get('name', rel_path),
                            'path': '/' + rel_path,
                            'type': 'dir' if item.get('type') == 'dir' else 'file',
                            'size': item.get('size', 0),
                            'mtime': item.get('mtime', '')
                        })
                else:
                    # Non-root path
                    norm_path = path.rstrip('/')
                    if item_path.startswith(norm_path + '/'):
                        rel = item_path[len(norm_path) + 1:]
                        if '/' in rel:
                            # Nested item, extract immediate child directory
                            child_dir = rel.split('/')[0]
                            child_path = norm_path + '/' + child_dir
                            if child_path not in seen_paths:
                                seen_paths.add(child_path)
                                files.append({
                                    'name': child_dir,
                                    'path': child_path,
                                    'type': 'dir',
                                    'size': 0,
                                    'mtime': ''
                                })
                        elif rel and item_path not in seen_paths:
                            seen_paths.add(item_path)
                            files.append({
                                'name': item.get('name', rel),
                                'path': item_path,
                                'type': 'dir' if item.get('type') == 'dir' else 'file',
                                'size': item.get('size', 0),
                                'mtime': item.get('mtime', '')
                            })
            except json.JSONDecodeError:
                continue

        # Sort: directories first, then by name
        files.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))

        return jsonify({
            'files': files,
            'path': path,
            'snapshot_id': snapshot_id
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/jobs/<job_id>/snapshots/<snapshot_id>/download')
@login_required
def api_snapshot_download(job_id, snapshot_id):
    """Download a file from a snapshot"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    file_path = request.args.get('path', '')
    if not file_path:
        return jsonify({'error': 'File path is required'}), 400

    # Sanitize path - prevent path traversal attacks
    # Normalize and ensure path doesn't escape intended directory
    normalized_path = os.path.normpath(file_path)
    if '..' in normalized_path or normalized_path.startswith('/etc') or normalized_path.startswith('/root'):
        return jsonify({'error': 'Invalid path'}), 400

    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    try:
        import tempfile
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        # Use restic dump to get file contents
        cmd = ['restic', 'dump', snapshot_id, file_path]
        if skip_ssl_verify:
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            timeout=300  # 5 minute timeout for downloads
        )

        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8', errors='replace') if result.stderr else 'Failed to download file'
            return jsonify({'error': error_msg}), 400

        # Get filename from path
        filename = os.path.basename(file_path)

        # Return file as attachment
        from flask import Response
        return Response(
            result.stdout,
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': len(result.stdout)
            }
        )

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Download timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/jobs/<job_id>/snapshots/<snapshot_id>/restore', methods=['POST'])
@login_required
@csrf.exempt
def api_snapshot_restore(job_id, snapshot_id):
    """Restore files from a snapshot to the server"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    source_path = data.get('source_path', '')
    target_path = data.get('target_path', '')

    if not source_path:
        return jsonify({'error': 'Source path is required'}), 400
    if not target_path:
        return jsonify({'error': 'Target path is required'}), 400

    # Sanitize paths
    if '..' in source_path or '..' in target_path:
        return jsonify({'error': 'Invalid path'}), 400

    # Get server info for SSH connection
    server_id = job.get('server_id')
    if not server_id:
        return jsonify({'error': 'Job has no associated server'}), 400

    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    try:
        # Build restic restore command
        repo = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        # Escape values for shell
        safe_snapshot = shlex.quote(snapshot_id)
        safe_target = shlex.quote(target_path)
        safe_source = shlex.quote(source_path)

        # Create the restic restore command - restore specific path to target
        insecure_flag = ' --insecure-tls' if skip_ssl_verify else ''
        restic_cmd = f"restic restore {safe_snapshot} --target {safe_target} --include {safe_source}{insecure_flag}"

        # Environment variables for restic
        full_cmd = f"""
export PATH="$HOME/.local/bin:$PATH"
export AWS_ACCESS_KEY_ID={shlex.quote(job['s3_access_key'])}
export AWS_SECRET_ACCESS_KEY={shlex.quote(job['s3_secret_key'])}
export RESTIC_PASSWORD={shlex.quote(job['restic_password'])}
export RESTIC_REPOSITORY={shlex.quote(repo)}
mkdir -p {safe_target}
{restic_cmd}
"""

        if not server.get('host'):
            return jsonify({'error': 'Server SSH configuration incomplete'}), 400

        ssh_cmd = _build_ssh_cmd_for_server(server, timeout=30)

        result = subprocess.run(
            ssh_cmd + [full_cmd],
            capture_output=True,
            text=True,
            timeout=1800
        )

        if result.returncode != 0:
            return jsonify({'error': result.stderr or 'Restore failed'}), 400

        return jsonify({
            'success': True,
            'message': f"Restored {source_path} to {target_path}",
            'output': result.stdout
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Restore timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/jobs/<job_id>/snapshots/<snapshot_id>/restore-db', methods=['POST'])
@login_required
@csrf.exempt
def api_snapshot_restore_db(job_id, snapshot_id):
    """Restore a database dump from a snapshot back into a database.

    Steps:
    1. Restore the snapshot's .sql.gz file to a temp dir on the jump server
    2. Locate the dump file (it's nested in the original temp path)
    3. Pipe it through zcat into mysql/psql against the target DB
    4. Clean up the temp dir
    """
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job.get('backup_type') != 'database':
        return jsonify({'error': 'Job is not a database backup'}), 400

    data = request.get_json() or {}
    # Allow overriding the target DB config, default to the job's configured one
    target_db_config_id = data.get('db_config_id') or job.get('database_config_id')
    target_database = data.get('target_database', '')  # Optional: restore into a specific DB name

    if not target_db_config_id:
        return jsonify({'error': 'No target database configuration'}), 400

    target_db = get_db_config(target_db_config_id)
    if not target_db:
        return jsonify({'error': 'Target database config not found'}), 404

    # Get server for SSH
    server_id = job.get('server_id')
    if not server_id:
        return jsonify({'error': 'Job has no associated server'}), 400

    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    # Ensure DB client is installed (skip when using docker exec — psql/mysql is inside the container)
    db_type = target_db.get('type', 'mysql')
    is_postgres = db_type in ('postgres', 'postgresql')
    client_type = 'postgres' if is_postgres else 'mysql'
    if (target_db.get('docker_container') or '').strip():
        ok, msg = True, "using docker exec"
    else:
        ok, msg = ensure_db_client_installed(server, client_type)
    if not ok:
        return jsonify({'error': f'{client_type} client not available on jump server: {msg}'}), 400

    # Get skip_ssl_verify
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    try:
        repo = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"
        insecure_flag = ' --insecure-tls' if skip_ssl_verify else ''

        # Escape values
        safe_snapshot = shlex.quote(snapshot_id)
        safe_db_host = shlex.quote(target_db['host'])
        safe_db_user = shlex.quote(target_db['username'])
        safe_db_pass = shlex.quote(target_db['password'])
        default_port = 5432 if is_postgres else 3306
        db_port = int(target_db.get('port', default_port))

        # Build the import command based on DB type
        # Check if the target DB uses docker exec
        container = target_db.get('docker_container') or ''
        use_docker = bool(container.strip())
        safe_container = shlex.quote(container) if use_docker else ''

        if use_docker:
            # For docker-exec: leave import_cmd empty; detection + execution happens in the remote script body
            import_cmd = None
        elif is_postgres:
            if target_database:
                import_cmd = f'PGPASSWORD={safe_db_pass} psql -h {safe_db_host} -p {db_port} -U {safe_db_user} -d {shlex.quote(target_database)}'
            else:
                import_cmd = f'PGPASSWORD={safe_db_pass} psql -h {safe_db_host} -p {db_port} -U {safe_db_user} -d postgres'
        else:
            if target_database:
                import_cmd = f'mysql -h {safe_db_host} -P {db_port} -u {safe_db_user} -p{safe_db_pass} {shlex.quote(target_database)}'
            else:
                import_cmd = f'mysql -h {safe_db_host} -P {db_port} -u {safe_db_user} -p{safe_db_pass}'

        if use_docker:
            target_db_name_arg = shlex.quote(target_database) if target_database else '""'
            import_block = f"""
if docker ps >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo -n docker"; fi
DETECTED=$($DOCKER exec {safe_container} sh -c "ps aux 2>/dev/null || ps 2>/dev/null" | tr '[:upper:]' '[:lower:]')
TARGET_DB={target_db_name_arg}
if echo "$DETECTED" | grep -qE "mysqld|mariadb"; then
    echo "Detected MySQL/MariaDB in container"
    if [ -n "$TARGET_DB" ]; then
        zcat "$DUMP_FILE" | $DOCKER exec -i {safe_container} mysql "$TARGET_DB"
    else
        zcat "$DUMP_FILE" | $DOCKER exec -i {safe_container} mysql
    fi
elif echo "$DETECTED" | grep -q "postgres"; then
    echo "Detected PostgreSQL in container"
    if [ -n "$TARGET_DB" ]; then
        zcat "$DUMP_FILE" | $DOCKER exec -i {safe_container} psql -U postgres -d "$TARGET_DB"
    else
        zcat "$DUMP_FILE" | $DOCKER exec -i {safe_container} psql -U postgres
    fi
else
    echo "Could not detect database type in container {container}" >&2
    exit 2
fi
IMPORT_EXIT=$?
"""
        else:
            import_block = f"""
echo "Importing into database..."
zcat "$DUMP_FILE" | {import_cmd}
IMPORT_EXIT=$?
"""

        remote_cmd = f"""
export PATH="$HOME/.local/bin:$PATH"
export AWS_ACCESS_KEY_ID={shlex.quote(job['s3_access_key'])}
export AWS_SECRET_ACCESS_KEY={shlex.quote(job['s3_secret_key'])}
export RESTIC_PASSWORD={shlex.quote(job['restic_password'])}
export RESTIC_REPOSITORY={shlex.quote(repo)}

RESTORE_DIR=$(mktemp -d)
trap 'rm -rf "$RESTORE_DIR"' EXIT

echo "Restoring snapshot {safe_snapshot} from repository..."
restic restore {safe_snapshot} --target "$RESTORE_DIR"{insecure_flag}
if [ $? -ne 0 ]; then
    echo "Failed to restore snapshot from restic"
    exit 1
fi

# Find the .sql.gz file - restic restores with original paths
DUMP_FILE=$(find "$RESTORE_DIR" -name "*.sql.gz" -type f | head -1)
if [ -z "$DUMP_FILE" ]; then
    echo "No .sql.gz dump file found in snapshot"
    find "$RESTORE_DIR" -type f
    exit 1
fi

echo "Found dump: $DUMP_FILE"
{import_block}

if [ $IMPORT_EXIT -ne 0 ]; then
    echo "Database import failed with exit code $IMPORT_EXIT"
    exit $IMPORT_EXIT
fi

echo "Database restore completed successfully"
"""

        ssh_cmd = _build_ssh_cmd_for_server(server, timeout=30)

        result = subprocess.run(
            ssh_cmd + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour for DB restore
        )

        if result.returncode != 0:
            error = sanitize_error_message(result.stderr or result.stdout or 'Database restore failed')
            logger.error(f"DB restore failed for job {job_id}: {error}")
            return jsonify({'error': error, 'output': result.stdout}), 400

        return jsonify({
            'success': True,
            'message': 'Database restored successfully',
            'output': result.stdout
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Database restore timed out'}), 400
    except Exception as e:
        logger.exception("Database restore error")
        return jsonify({'error': sanitize_error_message(str(e))}), 500


@app.route('/api/jobs/<job_id>/snapshots/<snapshot_id>/download-zip')
@login_required
def api_snapshot_download_zip(job_id, snapshot_id):
    """Download files/folders from a snapshot as a ZIP archive"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    file_path = request.args.get('path', '')
    if not file_path:
        return jsonify({'error': 'Path is required'}), 400

    # Sanitize path - prevent path traversal attacks
    normalized_path = os.path.normpath(file_path)
    if '..' in normalized_path or normalized_path.startswith('/etc') or normalized_path.startswith('/root'):
        return jsonify({'error': 'Invalid path'}), 400

    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    try:
        import tempfile
        import zipfile
        import shutil

        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        # Create a temporary directory for restore
        with tempfile.TemporaryDirectory() as temp_dir:
            # Use restic restore to extract files to temp directory
            cmd = ['restic', 'restore', snapshot_id, '--target', temp_dir, '--include', file_path]
            if skip_ssl_verify:
                cmd.append('--insecure-tls')

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=600  # 10 minute timeout
            )

            if result.returncode != 0:
                error_msg = result.stderr or 'Failed to extract files'
                return jsonify({'error': error_msg}), 400

            # Create ZIP file
            zip_path = tempfile.mktemp(suffix='.zip')
            archive_name = os.path.basename(file_path.rstrip('/')) or 'snapshot'

            # Find the extracted content
            extracted_path = os.path.join(temp_dir, file_path.lstrip('/'))

            if not os.path.exists(extracted_path):
                # Try without leading slash
                extracted_path = os.path.join(temp_dir, file_path.strip('/'))

            if not os.path.exists(extracted_path):
                return jsonify({'error': 'Failed to locate extracted files'}), 400

            # Create ZIP archive
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if os.path.isfile(extracted_path):
                    zipf.write(extracted_path, os.path.basename(extracted_path))
                else:
                    for root, dirs, files in os.walk(extracted_path):
                        for file in files:
                            file_full_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_full_path, extracted_path)
                            zipf.write(file_full_path, os.path.join(archive_name, arcname))

            # Read ZIP file and return as response
            with open(zip_path, 'rb') as f:
                zip_data = f.read()

            os.unlink(zip_path)

            from flask import Response
            return Response(
                zip_data,
                mimetype='application/zip',
                headers={
                    'Content-Disposition': f'attachment; filename="{archive_name}.zip"',
                    'Content-Length': len(zip_data)
                }
            )

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Download timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/history')
@login_required
def api_history():
    """Get backup history"""
    history = load_history()
    return jsonify(history)


@app.route('/api/dashboard/stats')
@login_required
def api_dashboard_stats():
    """Get dashboard statistics"""
    from datetime import datetime, timedelta

    jobs = load_jobs()
    history = load_history()

    # Basic job stats
    total_jobs = len(jobs)
    jobs_list = list(jobs.values())
    success_jobs = sum(1 for j in jobs_list if j.get('status') == 'success')
    failed_jobs = sum(1 for j in jobs_list if j.get('status') == 'failed')
    running_jobs = sum(1 for j in jobs_list if j.get('status') == 'running')
    scheduled_jobs = sum(1 for j in jobs_list if j.get('schedule_enabled'))

    # Calculate success rate from last 7 days history
    now = datetime.now()
    seven_days_ago = now - timedelta(days=7)
    recent_history = []
    for entry in history:
        try:
            entry_time = datetime.fromisoformat(entry.get('timestamp', '').replace('Z', '+00:00'))
            if entry_time.replace(tzinfo=None) >= seven_days_ago:
                recent_history.append(entry)
        except (ValueError, TypeError):
            pass

    total_recent = len(recent_history)
    successful_recent = sum(1 for e in recent_history if e.get('status') == 'success')
    success_rate = round((successful_recent / total_recent * 100) if total_recent > 0 else 0, 1)

    # Last 24 hours summary
    twenty_four_hours_ago = now - timedelta(hours=24)
    last_24h = []
    for entry in history:
        try:
            entry_time = datetime.fromisoformat(entry.get('timestamp', '').replace('Z', '+00:00'))
            if entry_time.replace(tzinfo=None) >= twenty_four_hours_ago:
                last_24h.append(entry)
        except (ValueError, TypeError):
            pass

    last_24h_success = sum(1 for e in last_24h if e.get('status') == 'success')
    last_24h_failed = sum(1 for e in last_24h if e.get('status') == 'failed')

    # Daily breakdown for last 7 days (for chart)
    daily_stats = []
    for i in range(6, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        day_entries = []
        for entry in history:
            try:
                entry_time = datetime.fromisoformat(entry.get('timestamp', '').replace('Z', '+00:00'))
                entry_time_naive = entry_time.replace(tzinfo=None)
                if day_start <= entry_time_naive < day_end:
                    day_entries.append(entry)
            except (ValueError, TypeError):
                pass

        daily_stats.append({
            'date': day_start.strftime('%Y-%m-%d'),
            'day': day_start.strftime('%a'),
            'success': sum(1 for e in day_entries if e.get('status') == 'success'),
            'failed': sum(1 for e in day_entries if e.get('status') == 'failed'),
            'total': len(day_entries)
        })

    # Next scheduled backup
    next_backup = None
    next_backup_job = None
    for job_id, job in jobs.items():
        if job.get('schedule_enabled') and job.get('schedule_cron'):
            try:
                from croniter import croniter
                cron = croniter(job['schedule_cron'], now)
                job_next = cron.get_next(datetime)
                if next_backup is None or job_next < next_backup:
                    next_backup = job_next
                    next_backup_job = job.get('name', job_id)
            except Exception:
                pass

    # Average backup duration (from successful backups)
    durations = [e.get('duration', 0) for e in recent_history if e.get('status') == 'success' and e.get('duration')]
    avg_duration = round(sum(durations) / len(durations)) if durations else 0

    # Total snapshots count - count all successful backups in history
    # Each successful backup creates one snapshot
    total_snapshots = sum(1 for e in history if e.get('status') == 'success')

    # Contribution data for GitHub-style graph (last 365 days)
    contribution_data = []
    for i in range(364, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        day_success = 0
        day_failed = 0
        for entry in history:
            try:
                entry_time = datetime.fromisoformat(entry.get('timestamp', '').replace('Z', '+00:00'))
                entry_time_naive = entry_time.replace(tzinfo=None)
                if day_start <= entry_time_naive < day_end:
                    if entry.get('status') == 'success':
                        day_success += 1
                    elif entry.get('status') == 'failed':
                        day_failed += 1
            except (ValueError, TypeError):
                pass

        contribution_data.append({
            'date': day_start.strftime('%Y-%m-%d'),
            'success': day_success,
            'failed': day_failed,
            'total': day_success + day_failed
        })

    return jsonify({
        'total_jobs': total_jobs,
        'success_jobs': success_jobs,
        'failed_jobs': failed_jobs,
        'running_jobs': running_jobs,
        'scheduled_jobs': scheduled_jobs,
        'success_rate': success_rate,
        'success_rate_period': '7 days',
        'last_24h': {
            'success': last_24h_success,
            'failed': last_24h_failed,
            'total': len(last_24h)
        },
        'daily_stats': daily_stats,
        'contribution_data': contribution_data,
        'next_backup': next_backup.isoformat() if next_backup else None,
        'next_backup_job': next_backup_job,
        'avg_duration': avg_duration,
        'total_snapshots': total_snapshots
    })


# S3 Configuration API Routes
@app.route('/api/s3-configs', methods=['GET'])
@login_required
@limiter.exempt
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
def api_create_s3_config_route():
    """Create a new S3 configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required_fields = ['name', 'endpoint', 'bucket', 'access_key', 'secret_key']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    # Validate endpoint format
    if not validate_s3_endpoint(data['endpoint']):
        return jsonify({'error': 'Invalid S3 endpoint format'}), 400

    # Validate bucket name
    if not validate_bucket_name(data['bucket']):
        return jsonify({'error': 'Invalid bucket name (3-63 chars, lowercase, alphanumeric, hyphens, periods)'}), 400

    # Sanitize name (prevent XSS)
    name = data['name'].strip()[:100]  # Limit length
    if not name:
        return jsonify({'error': 'name is required'}), 400

    new_config = {
        'id': generate_id(),
        'name': name,
        'endpoint': data['endpoint'].strip(),
        'bucket': data['bucket'].strip().lower(),
        'access_key': data['access_key'].strip(),
        'secret_key': data['secret_key'],
        'region': data.get('region', '').strip(),
        'skip_ssl_verify': bool(data.get('skip_ssl_verify', False)),
        'status': data.get('status', 'active'),
        'created_at': utc_isoformat(),
        'updated_at': utc_isoformat()
    }

    create_s3_config(new_config)

    # Return safe version
    safe_config = {**new_config}
    safe_config['secret_key'] = '********'
    return jsonify(safe_config), 201


@app.route('/api/s3-configs/<config_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_s3_config_route(config_id):
    """Update an S3 configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    config = get_s3_config(config_id)
    if not config:
        return jsonify({'error': 'Configuration not found'}), 404

    # Update fields
    config['name'] = data.get('name', config['name'])
    config['endpoint'] = data.get('endpoint', config['endpoint'])
    config['bucket'] = data.get('bucket', config['bucket'])
    config['access_key'] = data.get('access_key', config['access_key'])
    config['region'] = data.get('region', config.get('region', ''))
    config['skip_ssl_verify'] = data.get('skip_ssl_verify', config.get('skip_ssl_verify', False))
    config['status'] = data.get('status', config.get('status', 'active'))

    # Only update secret_key if provided and not empty
    if data.get('secret_key'):
        config['secret_key'] = data['secret_key']

    update_s3_config(config_id, config)

    # Return safe version
    safe_config = {**config}
    safe_config['secret_key'] = '********'
    return jsonify(safe_config)


@app.route('/api/s3-configs/<config_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_s3_config_route(config_id):
    """Delete an S3 configuration"""
    config = get_s3_config(config_id)
    if not config:
        return jsonify({'error': 'Configuration not found'}), 404

    delete_s3_config(config_id)

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
    skip_ssl_verify = data.get('skip_ssl_verify', False)
    config_id = data.get('id', '')

    # If editing existing config and secret_key is empty, look up the stored one
    if config_id and not secret_key:
        existing_config = get_s3_config(config_id)
        if existing_config:
            secret_key = existing_config.get('secret_key', '')

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

        # Build command with optional SSL skip
        cmd = ['rclone', 'lsd', f'test:{bucket}', '--max-depth', '1']
        if skip_ssl_verify:
            cmd.append('--no-check-certificate')

        result = subprocess.run(
            cmd,
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


@app.route('/api/s3-configs/<config_id>/browse', methods=['GET'])
@login_required
def api_browse_s3_config(config_id):
    """Browse S3 bucket contents"""
    config = get_s3_config(config_id)
    if not config:
        return jsonify({'error': 'S3 configuration not found'}), 404

    path = request.args.get('path', '')

    # Sanitize path - remove leading/trailing slashes and prevent directory traversal
    path = path.strip('/')
    if '..' in path:
        return jsonify({'error': 'Invalid path'}), 400

    try:
        # Use rclone to list bucket contents
        env = os.environ.copy()
        env['RCLONE_CONFIG_BROWSE_TYPE'] = 's3'
        env['RCLONE_CONFIG_BROWSE_PROVIDER'] = 'Other'
        env['RCLONE_CONFIG_BROWSE_ACCESS_KEY_ID'] = config['access_key']
        env['RCLONE_CONFIG_BROWSE_SECRET_ACCESS_KEY'] = config['secret_key']
        env['RCLONE_CONFIG_BROWSE_ENDPOINT'] = f"https://{config['endpoint']}"
        env['RCLONE_CONFIG_BROWSE_REGION'] = config.get('region', 'us-east-1')

        # Build the remote path
        remote_path = f"browse:{config['bucket']}"
        if path:
            remote_path = f"{remote_path}/{path}"

        # Use lsjson for structured output
        cmd = ['rclone', 'lsjson', remote_path, '--max-depth', '1']
        if config.get('skip_ssl_verify'):
            cmd.append('--no-check-certificate')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )

        if result.returncode != 0:
            return jsonify({'error': result.stderr or 'Failed to list bucket contents'}), 400

        # Parse rclone lsjson output
        import json as json_module
        items = json_module.loads(result.stdout) if result.stdout.strip() else []

        # Transform to our format
        objects = []
        for item in items:
            objects.append({
                'name': item.get('Name', ''),
                'path': f"{path}/{item.get('Name', '')}" if path else item.get('Name', ''),
                'size': item.get('Size', 0),
                'is_dir': item.get('IsDir', False),
                'mod_time': item.get('ModTime', '')
            })

        # Sort: directories first, then by name
        objects.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))

        return jsonify({
            'objects': objects,
            'path': path,
            'bucket': config['bucket'],
            'config_name': config['name']
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/s3-configs/<config_id>/download', methods=['GET'])
@login_required
def api_download_s3_file(config_id):
    """Download a file from S3 bucket"""
    config = get_s3_config(config_id)
    if not config:
        return jsonify({'error': 'S3 configuration not found'}), 404

    file_path = request.args.get('path', '')

    if not file_path:
        return jsonify({'error': 'File path is required'}), 400

    # Sanitize path - prevent directory traversal
    file_path = file_path.strip('/')
    if '..' in file_path:
        return jsonify({'error': 'Invalid path'}), 400

    try:
        # Use rclone to download file to a temp location
        import tempfile
        env = os.environ.copy()
        env['RCLONE_CONFIG_DL_TYPE'] = 's3'
        env['RCLONE_CONFIG_DL_PROVIDER'] = 'Other'
        env['RCLONE_CONFIG_DL_ACCESS_KEY_ID'] = config['access_key']
        env['RCLONE_CONFIG_DL_SECRET_ACCESS_KEY'] = config['secret_key']
        env['RCLONE_CONFIG_DL_ENDPOINT'] = f"https://{config['endpoint']}"
        env['RCLONE_CONFIG_DL_REGION'] = config.get('region', 'us-east-1')

        # Create temp file for download
        filename = os.path.basename(file_path)
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, filename)

        # Build the remote path
        remote_path = f"dl:{config['bucket']}/{file_path}"

        # Download file using rclone
        cmd = ['rclone', 'copy', remote_path, temp_dir]
        if config.get('skip_ssl_verify'):
            cmd.append('--no-check-certificate')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=300  # 5 minute timeout for downloads
        )

        if result.returncode != 0:
            # Clean up temp dir
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'error': result.stderr or 'Failed to download file'}), 400

        if not os.path.exists(temp_file):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'error': 'File not found'}), 404

        # Send file and clean up after
        from flask import send_file, after_this_request

        @after_this_request
        def cleanup(response):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            return response

        return send_file(
            temp_file,
            as_attachment=True,
            download_name=filename
        )

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Download timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Server API Routes
@app.route('/api/servers', methods=['GET'])
@login_required
@limiter.exempt
def api_get_servers():
    """Get all servers"""
    servers = load_servers()
    # Strip sensitive fields
    safe_servers = []
    for s in servers:
        safe = {**s}
        safe.pop('ssh_password', None)
        safe.pop('ssh_key_content', None)
        safe_servers.append(safe)
    return jsonify(safe_servers)


@app.route('/api/servers', methods=['POST'])
@login_required
@csrf.exempt
def api_create_server_route():
    """Create a new server"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Required fields for all servers
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    if not data.get('host'):
        return jsonify({'error': 'host is required'}), 400

    # Validate hostname
    if not validate_hostname(data['host']):
        return jsonify({'error': 'Invalid hostname or IP address'}), 400

    if not data.get('ssh_user'):
        return jsonify({'error': 'ssh_user is required'}), 400

    ssh_port = int(data.get('ssh_port', 22))
    if not validate_port(ssh_port):
        return jsonify({'error': 'Invalid SSH port (must be 1-65535)'}), 400

    ssh_auth_type = data.get('ssh_auth_type', 'key_path')
    if ssh_auth_type not in ('key_path', 'key_content', 'password'):
        return jsonify({'error': 'Invalid ssh_auth_type'}), 400

    ssh_key = data.get('ssh_key', '/home/backupx/.ssh/id_rsa')
    if ssh_auth_type == 'key_path':
        if not validate_path(ssh_key):
            return jsonify({'error': 'Invalid SSH key path'}), 400

    # Encrypt sensitive fields
    ssh_password = None
    if ssh_auth_type == 'password':
        ssh_password = data.get('ssh_password', '')
        if not ssh_password:
            return jsonify({'error': 'SSH password is required for password authentication'}), 400
        ssh_password = encrypt_credential(ssh_password)

    ssh_key_content = None
    if ssh_auth_type == 'key_content':
        ssh_key_content = data.get('ssh_key_content', '')
        if not ssh_key_content:
            return jsonify({'error': 'SSH key content is required'}), 400
        ssh_key_content = encrypt_credential(ssh_key_content)

    new_server = {
        'id': generate_id(),
        'name': data['name'],
        'host': data['host'],
        'ssh_port': ssh_port,
        'ssh_user': data['ssh_user'],
        'ssh_key': ssh_key,
        'ssh_auth_type': ssh_auth_type,
        'ssh_password': ssh_password,
        'ssh_key_content': ssh_key_content,
        'status': data.get('status', 'active'),
        'created_at': utc_isoformat(),
        'updated_at': utc_isoformat()
    }

    create_server(new_server)

    # Auto-install restic on remote server in background
    def _provision(srv):
        host = srv['host']
        ssh_user = srv.get('ssh_user', 'root')
        auth_type = srv.get('ssh_auth_type', 'key_path')
        ssh_pw = srv.get('ssh_password', '')
        if ssh_pw and is_encrypted(ssh_pw):
            ssh_pw = decrypt_credential(ssh_pw)
        success, msg = ensure_restic_installed(host, ssh_user,
            int(srv.get('ssh_port') or 22),
            _get_ssh_key_path(srv),
            ssh_auth_type=auth_type, ssh_password=ssh_pw)
        if success:
            logger.info(f"Auto-provisioned restic on {ssh_user}@{host}: {msg}")
        else:
            logger.warning(f"Failed to auto-provision restic on {ssh_user}@{host}: {msg}")
    threading.Thread(
        target=_provision,
        args=(new_server,),
        daemon=True
    ).start()

    # Don't return encrypted values
    safe_server = {**new_server}
    safe_server.pop('ssh_password', None)
    safe_server.pop('ssh_key_content', None)
    return jsonify(safe_server), 201


@app.route('/api/servers/<server_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_server_route(server_id):
    """Update a server"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    server['name'] = data.get('name', server['name'])
    server['host'] = data.get('host', server['host'])
    server['status'] = data.get('status', server.get('status', 'active'))
    server['ssh_port'] = int(data.get('ssh_port', server.get('ssh_port', 22)))
    server['ssh_user'] = data.get('ssh_user', server.get('ssh_user'))
    server['ssh_key'] = data.get('ssh_key', server.get('ssh_key', '/home/backupx/.ssh/id_rsa'))
    server['ssh_auth_type'] = data.get('ssh_auth_type', server.get('ssh_auth_type', 'key_path'))

    # Update password if provided
    if 'ssh_password' in data and data['ssh_password']:
        server['ssh_password'] = encrypt_credential(data['ssh_password'])
    # Update key content if provided
    if 'ssh_key_content' in data and data['ssh_key_content']:
        server['ssh_key_content'] = encrypt_credential(data['ssh_key_content'])

    update_server_in_db(server_id, server)

    # Auto-install restic on remote server in background
    if server.get('ssh_user') and server.get('host'):
        def _provision(srv):
            auth_type = srv.get('ssh_auth_type', 'key_path')
            ssh_pw = srv.get('ssh_password', '')
            if ssh_pw and is_encrypted(ssh_pw):
                ssh_pw = decrypt_credential(ssh_pw)
            success, msg = ensure_restic_installed(
                srv['host'], srv.get('ssh_user', 'root'),
                int(srv.get('ssh_port') or 22),
                _get_ssh_key_path(srv),
                ssh_auth_type=auth_type, ssh_password=ssh_pw)
            if success:
                logger.info(f"Auto-provisioned restic on {srv.get('ssh_user')}@{srv['host']}: {msg}")
            else:
                logger.warning(f"Failed to auto-provision restic on {srv.get('ssh_user')}@{srv['host']}: {msg}")
        threading.Thread(
            target=_provision,
            args=(server,),
            daemon=True
        ).start()

    # Don't return encrypted values
    safe_server = {**server}
    safe_server.pop('ssh_password', None)
    safe_server.pop('ssh_key_content', None)
    return jsonify(safe_server)


@app.route('/api/servers/<server_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_server_route(server_id):
    """Delete a server"""
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    delete_server_from_db(server_id)

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
    if not host:
        return jsonify({'error': 'Host is required'}), 400

    ssh_port = int(data.get('ssh_port', 22))
    ssh_user = data.get('ssh_user', '')
    ssh_key = data.get('ssh_key', '/home/backupx/.ssh/id_rsa')
    ssh_auth_type = data.get('ssh_auth_type', 'key_path')
    ssh_password = data.get('ssh_password', '')

    if not ssh_user:
        return jsonify({'error': 'SSH user is required'}), 400

    # For edits: if password/key_content is blank, fall back to stored values
    existing_server_id = data.get('id', '')
    if existing_server_id:
        existing = get_server(existing_server_id)
        if existing:
            if ssh_auth_type == 'password' and not ssh_password:
                stored_pw = existing.get('ssh_password', '')
                if stored_pw and is_encrypted(stored_pw):
                    ssh_password = decrypt_credential(stored_pw)
                else:
                    ssh_password = stored_pw
            if ssh_auth_type == 'key_content' and not data.get('ssh_key_content'):
                stored_key = existing.get('ssh_key_content', '')
                if stored_key and is_encrypted(stored_key):
                    stored_key = decrypt_credential(stored_key)
                if stored_key:
                    ssh_key = _write_server_key_file(existing_server_id, stored_key)

    # Handle key_content: write to temp file
    if ssh_auth_type == 'key_content' and not existing_server_id:
        key_content = data.get('ssh_key_content', '')
        if key_content:
            ssh_key = _write_server_key_file('test_connection', key_content)

    try:
        ssh_cmd = _build_ssh_cmd(host, ssh_user, ssh_port, ssh_key, timeout=10,
                                  ssh_auth_type=ssh_auth_type, ssh_password=ssh_password)

        result = subprocess.run(
            ssh_cmd + ['export PATH="$HOME/.local/bin:$PATH"; echo "Connection successful" && (restic version 2>/dev/null || echo "RESTIC_NOT_FOUND")'],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            output = result.stdout.strip()
            restic_installed = 'RESTIC_NOT_FOUND' not in output
            restic_version = None
            if restic_installed:
                for line in output.split('\n'):
                    if line.startswith('restic'):
                        restic_version = line.strip()
                        break
            return jsonify({
                'success': True,
                'message': 'SSH connection successful',
                'restic_installed': restic_installed,
                'restic_version': restic_version
            })
        else:
            return jsonify({'error': result.stderr or 'SSH connection failed'}), 400

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'SSH connection timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/servers/<server_id>/test', methods=['POST'])
@login_required
@csrf.exempt
def api_test_server_connection_by_id(server_id):
    """Test connection to a saved server by ID"""
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    try:
        ssh_cmd = _build_ssh_cmd_for_server(server, timeout=10)

        result = subprocess.run(
            ssh_cmd + ['export PATH="$HOME/.local/bin:$PATH"; echo "Connection successful" && (restic version 2>/dev/null || echo "RESTIC_NOT_FOUND")'],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            output = result.stdout.strip()
            restic_installed = 'RESTIC_NOT_FOUND' not in output
            restic_version = None
            if restic_installed:
                for line in output.split('\n'):
                    if line.startswith('restic'):
                        restic_version = line.strip()
                        break
            return jsonify({
                'success': True,
                'status': 'online',
                'message': 'SSH connection successful',
                'restic_installed': restic_installed,
                'restic_version': restic_version
            })
        else:
            return jsonify({'success': False, 'status': 'offline', 'error': result.stderr or 'SSH connection failed'})

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'status': 'offline', 'error': 'SSH connection timed out'})
    except Exception as e:
        return jsonify({'success': False, 'status': 'error', 'error': str(e)})


@app.route('/api/servers/<server_id>/install-restic', methods=['POST'])
@login_required
@csrf.exempt
def api_install_restic(server_id):
    """Install restic on a remote server via SSH"""
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    host = server.get('host', '')
    ssh_user = server.get('ssh_user', '')
    ssh_port = int(server.get('ssh_port') or 22)

    if not host or not ssh_user:
        return jsonify({'error': 'Server SSH configuration incomplete'}), 400

    # Clear cache for this server so it re-checks
    cache_key = f"{ssh_user}@{host}:{ssh_port}"
    _restic_confirmed.discard(cache_key)

    auth_type = server.get('ssh_auth_type', 'key_path')
    ssh_password = server.get('ssh_password', '')
    if ssh_password and is_encrypted(ssh_password):
        ssh_password = decrypt_credential(ssh_password)
    success, message = ensure_restic_installed(host, ssh_user, ssh_port,
        _get_ssh_key_path(server),
        ssh_auth_type=auth_type, ssh_password=ssh_password)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 400


@app.route('/api/servers/<server_id>/db-containers', methods=['GET'])
@login_required
def api_list_db_containers(server_id):
    """List running database containers on a remote server via SSH.

    Detects MySQL/MariaDB and PostgreSQL containers by running `docker ps`
    and checking processes inside each container.
    """
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    if not server.get('host') or not server.get('ssh_user'):
        return jsonify({'error': 'Server SSH configuration incomplete'}), 400

    try:
        ssh_cmd = _build_ssh_cmd_for_server(server, timeout=15)

        # Use docker via direct call if possible, else sudo -n
        remote_cmd = '''
DOCKER=""
for path in docker /usr/bin/docker /usr/local/bin/docker /snap/bin/docker; do
    if command -v "$path" >/dev/null 2>&1 || [ -x "$path" ]; then
        DOCKER=$(command -v "$path" 2>/dev/null || echo "$path")
        break
    fi
done

if [ -z "$DOCKER" ]; then
    echo "DOCKER_NOT_FOUND"
    exit 1
fi

if $DOCKER ps >/dev/null 2>&1; then
    DOCKER_CMD="$DOCKER"
elif sudo -n $DOCKER ps >/dev/null 2>&1; then
    DOCKER_CMD="sudo -n $DOCKER"
else
    echo "DOCKER_NO_ACCESS"
    $DOCKER ps 2>&1 >&2
    sudo -n $DOCKER ps 2>&1 >&2
    exit 1
fi

# List running containers + detect DB type
# Use multiple signals: image name, /proc/1/comm, ps aux output, env vars
for container in $($DOCKER_CMD ps --format '{{.Names}}'); do
    image=$($DOCKER_CMD inspect --format='{{.Config.Image}}' "$container" 2>/dev/null || echo "unknown")
    image_lc=$(echo "$image" | tr '[:upper:]' '[:lower:]')

    # Signal 1: image name
    db_type=""
    case "$image_lc" in
        *mysql*|*mariadb*|*percona*) db_type="mysql" ;;
        *postgres*|*postgis*|*timescale*) db_type="postgres" ;;
    esac

    # Signal 2: /proc/1/comm (the main process name)
    if [ -z "$db_type" ]; then
        comm=$($DOCKER_CMD exec "$container" cat /proc/1/comm 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")
        case "$comm" in
            *mysqld*|*mariadb*) db_type="mysql" ;;
            *postgres*|*postmaster*) db_type="postgres" ;;
        esac
    fi

    # Signal 3: ps aux inside the container
    if [ -z "$db_type" ]; then
        procs=$($DOCKER_CMD exec "$container" sh -c "ps aux 2>/dev/null || ps 2>/dev/null" 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")
        if echo "$procs" | grep -qE "mysqld|mariadb"; then
            db_type="mysql"
        elif echo "$procs" | grep -q "postgres"; then
            db_type="postgres"
        fi
    fi

    if [ -n "$db_type" ]; then
        echo "DB|$container|$db_type|$image"
    fi
done
'''

        result = subprocess.run(
            ssh_cmd + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=60
        )

        output = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()

        if 'DOCKER_NOT_FOUND' in output:
            return jsonify({'error': 'docker CLI not found on server', 'containers': []}), 400
        if 'DOCKER_NO_ACCESS' in output:
            debug = stderr[-500:] if stderr else ''
            return jsonify({
                'error': 'SSH user cannot access docker daemon (tried direct + sudo -n). Add the user to the docker group, or allow passwordless sudo for docker.',
                'debug': debug,
                'containers': []
            }), 400

        containers = []
        for line in output.split('\n'):
            line = line.strip()
            if not line.startswith('DB|'):
                continue
            parts = line.split('|', 3)
            if len(parts) < 4:
                continue
            _, name, db_type, image = parts
            containers.append({
                'name': name,
                'type': db_type,
                'image': image,
            })

        return jsonify({'containers': containers})

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Listing containers timed out', 'containers': []}), 504
    except Exception as e:
        return jsonify({'error': str(e), 'containers': []}), 500


@app.route('/api/servers/<server_id>/browse', methods=['GET'])
@login_required
def api_browse_server_directories(server_id):
    """Browse directories on a remote server via SSH"""
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    path = request.args.get('path', '/')

    # Validate path
    if not path.startswith('/'):
        return jsonify({'error': 'Path must be absolute'}), 400
    if '..' in path.split('/'):
        return jsonify({'error': 'Path traversal not allowed'}), 400

    if not server.get('host') or not server.get('ssh_user'):
        return jsonify({'error': 'Server SSH configuration incomplete'}), 400

    try:
        ssh_cmd = _build_ssh_cmd_for_server(server, timeout=10)
        safe_path = shlex.quote(path)
        # List both directories and files, tagged with 'd' or 'f'
        remote_cmd = f"find {safe_path} -maxdepth 1 -mindepth 1 -printf '%y\\t%p\\n' 2>/dev/null | sort -t$'\\t' -k1,1 -k2,2 | head -1000"

        result = subprocess.run(
            ssh_cmd + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=15
        )

        if result.returncode != 0 and not result.stdout:
            return jsonify({'error': 'Failed to list directories'}), 500

        directories = []
        files = []
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if not line or '\t' not in line:
                continue
            entry_type, entry_path = line.split('\t', 1)
            entry = {
                'name': os.path.basename(entry_path),
                'path': entry_path,
                'type': 'directory' if entry_type == 'd' else 'file'
            }
            if entry_type == 'd':
                directories.append(entry)
            else:
                files.append(entry)

        return jsonify({'path': path, 'directories': directories, 'files': files})

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Connection timed out'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Database Configuration API Routes
@app.route('/api/databases', methods=['GET'])
@login_required
@limiter.exempt
def api_get_db_configs():
    """Get all database configurations"""
    configs = load_db_configs()
    # Hide passwords in response
    safe_configs = []
    for config in configs:
        safe_config = {**config}
        safe_config['password'] = '********' if config.get('password') else ''
        safe_configs.append(safe_config)
    return jsonify(safe_configs)


@app.route('/api/databases', methods=['POST'])
@login_required
@csrf.exempt
def api_create_db_config_route():
    """Create a new database configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # For docker-exec mode, only name + docker_container are required (auth happens via local socket inside the container)
    # For direct mode, host/username/password are required
    if (data.get('docker_container') or '').strip():
        required_fields = ['name']
    else:
        required_fields = ['name', 'host', 'username', 'password']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    new_config = {
        'id': generate_id(),
        'name': data['name'],
        'type': data.get('type', 'mysql'),
        'host': data.get('host') or '',
        'port': int(data.get('port') or 3306),
        'username': data.get('username') or '',
        'password': data.get('password') or '',
        'databases': data.get('databases') or '*',
        'docker_container': data.get('docker_container') or None,
        'status': data.get('status', 'active'),
        'created_at': utc_isoformat(),
        'updated_at': utc_isoformat()
    }

    create_db_config(new_config)

    # Return safe version
    safe_config = {**new_config}
    safe_config['password'] = '********'
    return jsonify(safe_config), 201


@app.route('/api/databases/<config_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_db_config_route(config_id):
    """Update a database configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    config = get_db_config(config_id)
    if not config:
        return jsonify({'error': 'Configuration not found'}), 404

    # Update fields
    config['name'] = data.get('name', config['name'])
    config['type'] = data.get('type', config.get('type', 'mysql'))
    config['host'] = data.get('host', config['host'])
    config['port'] = int(data.get('port', config.get('port', 3306)))
    config['username'] = data.get('username', config['username'])
    config['databases'] = data.get('databases', config.get('databases', '*'))
    config['docker_container'] = data.get('docker_container') if 'docker_container' in data else config.get('docker_container')
    config['status'] = data.get('status', config.get('status', 'active'))

    # Only update password if provided and not empty
    if data.get('password'):
        config['password'] = data['password']

    update_db_config_in_db(config_id, config)

    # Return safe version
    safe_config = {**config}
    safe_config['password'] = '********'
    return jsonify(safe_config)


@app.route('/api/databases/<config_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_db_config_route(config_id):
    """Delete a database configuration"""
    config = get_db_config(config_id)
    if not config:
        return jsonify({'error': 'Configuration not found'}), 404

    delete_db_config_from_db(config_id)

    return jsonify({'success': True})


@app.route('/api/databases/test', methods=['POST'])
@login_required
@csrf.exempt
def api_test_db_connection():
    """Test database connection via SSH (MySQL or PostgreSQL)"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Determine db type
    db_type = data.get('type', 'mysql')
    is_postgres = db_type in ('postgres', 'postgresql')
    default_port = 5432 if is_postgres else 3306

    # Database config
    db_host = data.get('host', '')
    db_port = int(data.get('port', default_port))
    db_user = data.get('username', '')
    db_pass = data.get('password', '')
    docker_container = (data.get('docker_container') or '').strip()
    use_docker = bool(docker_container)

    # Server config
    server_id = data.get('server_id', '')

    if not use_docker and (not db_user or not db_pass or not db_host):
        return jsonify({'error': 'Host, username, and password are required'}), 400

    if not server_id:
        return jsonify({'error': 'Server selection required to test connection'}), 400

    # Get server details
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 400

    # Ensure the right client is installed on the jump server (skipped for docker exec)
    client_type = 'postgres' if is_postgres else 'mysql'
    if not use_docker:
        ok, msg = ensure_db_client_installed(server, client_type)
        if not ok:
            return jsonify({'error': f'{client_type} client not available on server: {msg}'}), 400

    try:
        # Escape all values for shell to prevent command injection
        escaped_host = shlex.quote(db_host)
        escaped_user = shlex.quote(db_user)
        escaped_pass = shlex.quote(db_pass)
        escaped_container = shlex.quote(docker_container) if use_docker else ''

        # Build SSH command
        ssh_cmd = _build_ssh_cmd_for_server(server, timeout=10)

        if use_docker:
            # Pick direct docker or sudo -n docker, then auto-detect DB type and run SELECT 1
            test_cmd = (
                f'if docker ps >/dev/null 2>&1; then D="docker"; else D="sudo -n docker"; fi; '
                f'DETECTED=$($D exec {escaped_container} sh -c "ps aux 2>/dev/null || ps 2>/dev/null" | tr "[:upper:]" "[:lower:]"); '
                f'if echo "$DETECTED" | grep -qE "mysqld|mariadb"; then '
                f'  $D exec {escaped_container} mysql -e "SELECT 1" 2>&1 && echo "OK: MySQL/MariaDB"; '
                f'elif echo "$DETECTED" | grep -q "postgres"; then '
                f'  $D exec {escaped_container} psql -U postgres -c "SELECT 1" 2>&1 && echo "OK: PostgreSQL"; '
                f'else '
                f'  echo "Could not detect database type in container"; exit 2; '
                f'fi'
            )
        elif is_postgres:
            test_cmd = f"PGPASSWORD={escaped_pass} psql -h {escaped_host} -p {db_port} -U {escaped_user} -d postgres -c 'SELECT 1' 2>&1"
        else:
            test_cmd = f"mysql -h {escaped_host} -P {db_port} -u {escaped_user} -p{escaped_pass} -e 'SELECT 1' 2>&1"

        ssh_cmd.append(test_cmd)

        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            return jsonify({'success': True, 'message': 'Database connection successful'})
        else:
            error_msg = sanitize_error_message(result.stderr or result.stdout or 'Connection failed')
            return jsonify({'error': error_msg}), 400

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Connection timed out'}), 400
    except Exception as e:
        return jsonify({'error': sanitize_error_message(str(e))}), 500


# --- Notification Channel Endpoints ---

@app.route('/api/notifications', methods=['GET'])
@login_required
def api_get_notifications():
    """Get all notification channels"""
    channels = load_notification_channels()
    # Mask sensitive data in config
    for channel in channels:
        if channel['type'] == 'email':
            if 'smtp_password' in channel['config']:
                channel['config']['smtp_password'] = '********' if channel['config']['smtp_password'] else ''
    return jsonify(channels)


@app.route('/api/notifications', methods=['POST'])
@login_required
@csrf.exempt
def api_create_notification():
    """Create a new notification channel"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required = ['name', 'type', 'config']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    # Validate type
    valid_types = ['email', 'slack', 'discord', 'telegram', 'webhook']
    if data['type'] not in valid_types:
        return jsonify({'error': f'Invalid type. Must be one of: {", ".join(valid_types)}'}), 400

    import uuid
    channel = {
        'id': str(uuid.uuid4()),
        'name': data['name'],
        'type': data['type'],
        'enabled': data.get('enabled', True),
        'config': data['config'],
        'notify_on_success': data.get('notify_on_success', True),
        'notify_on_failure': data.get('notify_on_failure', True),
        'created_at': utc_isoformat()
    }

    create_notification_channel(channel)
    return jsonify({'success': True, 'id': channel['id']})


@app.route('/api/notifications/<channel_id>', methods=['PUT'])
@login_required
@csrf.exempt
def api_update_notification(channel_id):
    """Update a notification channel"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    existing = get_notification_channel(channel_id)
    if not existing:
        return jsonify({'error': 'Channel not found'}), 404

    # If password is masked, keep the existing password
    if data.get('type') == 'email' and data.get('config', {}).get('smtp_password') == '********':
        data['config']['smtp_password'] = existing['config'].get('smtp_password', '')

    channel = {
        'name': data.get('name', existing['name']),
        'type': data.get('type', existing['type']),
        'enabled': data.get('enabled', existing['enabled']),
        'config': data.get('config', existing['config']),
        'notify_on_success': data.get('notify_on_success', existing['notify_on_success']),
        'notify_on_failure': data.get('notify_on_failure', existing['notify_on_failure'])
    }

    update_notification_channel(channel_id, channel)
    return jsonify({'success': True})


@app.route('/api/notifications/<channel_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_notification(channel_id):
    """Delete a notification channel"""
    existing = get_notification_channel(channel_id)
    if not existing:
        return jsonify({'error': 'Channel not found'}), 404

    delete_notification_channel(channel_id)
    return jsonify({'success': True})


@app.route('/api/notifications/test', methods=['POST'])
@login_required
@csrf.exempt
def api_test_notification():
    """Send a test notification"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    channel_type = data.get('type')
    config = data.get('config', {})

    if not channel_type:
        return jsonify({'error': 'Channel type required'}), 400

    # Create a temporary channel object for testing
    channel = {
        'name': 'Test Channel',
        'type': channel_type,
        'config': config
    }

    try:
        if channel_type == 'email':
            send_email_notification(channel, 'Test Job', 'success', 'This is a test notification from BackupX', 0)
        elif channel_type == 'slack':
            send_slack_notification(channel, 'Test Job', 'success', 'This is a test notification from BackupX', 0)
        elif channel_type == 'discord':
            send_discord_notification(channel, 'Test Job', 'success', 'This is a test notification from BackupX', 0)
        elif channel_type == 'telegram':
            send_telegram_notification(channel, 'Test Job', 'success', 'This is a test notification from BackupX', 0)
        elif channel_type == 'webhook':
            send_webhook_notification(channel, 'Test Job', 'success', 'This is a test notification from BackupX', 0)
        else:
            return jsonify({'error': f'Unknown channel type: {channel_type}'}), 400

        return jsonify({'success': True, 'message': 'Test notification sent successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Audit Log API Endpoints
# =============================================================================

@app.route('/api/audit', methods=['GET'])
@login_required
def api_get_audit_logs():
    """Get audit log entries with filtering and pagination"""
    try:
        from .audit import get_audit_logger
        audit_logger = get_audit_logger()

        if not audit_logger:
            return jsonify({'error': 'Audit logging not initialized'}), 500

        # Parse query parameters
        limit = min(int(request.args.get('limit', 50)), 500)
        offset = int(request.args.get('offset', 0))
        user_id = request.args.get('user_id')
        action = request.args.get('action')
        resource_type = request.args.get('resource_type')
        resource_id = request.args.get('resource_id')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        status = request.args.get('status')

        # Get logs and count
        logs = audit_logger.get_logs(
            limit=limit,
            offset=offset,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            start_date=start_date,
            end_date=end_date,
            status=status
        )

        total = audit_logger.get_log_count(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            start_date=start_date,
            end_date=end_date,
            status=status
        )

        return jsonify({
            'logs': logs,
            'total': total,
            'limit': limit,
            'offset': offset
        })

    except Exception as e:
        logger.error(f"Error fetching audit logs: {e}")
        return jsonify({'error': 'Failed to fetch audit logs'}), 500


@app.route('/api/audit/export', methods=['GET'])
@login_required
def api_export_audit_logs():
    """Export audit logs as JSON or CSV"""
    try:
        from .audit import get_audit_logger
        audit_logger = get_audit_logger()

        if not audit_logger:
            return jsonify({'error': 'Audit logging not initialized'}), 500

        format_type = request.args.get('format', 'json').lower()
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        data = audit_logger.export(
            start_date=start_date,
            end_date=end_date,
            format=format_type
        )

        if format_type == 'csv':
            from flask import Response
            return Response(
                data,
                mimetype='text/csv',
                headers={'Content-Disposition': 'attachment; filename=audit_log.csv'}
            )
        else:
            return Response(
                data,
                mimetype='application/json',
                headers={'Content-Disposition': 'attachment; filename=audit_log.json'}
            )

    except Exception as e:
        logger.error(f"Error exporting audit logs: {e}")
        return jsonify({'error': 'Failed to export audit logs'}), 500


@app.route('/api/audit/stats', methods=['GET'])
@login_required
def api_audit_stats():
    """Get audit log statistics"""
    try:
        from .audit import get_audit_logger
        audit_logger = get_audit_logger()

        if not audit_logger:
            return jsonify({'error': 'Audit logging not initialized'}), 500

        # Get counts by action type
        stats = {
            'total': audit_logger.get_log_count(),
            'by_action': {},
            'by_status': {},
            'by_resource_type': {}
        }

        # Count by action
        for action in ['CREATE', 'UPDATE', 'DELETE', 'LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'RUN_BACKUP']:
            count = audit_logger.get_log_count(action=action)
            if count > 0:
                stats['by_action'][action] = count

        # Count by status
        for status in ['success', 'failure']:
            count = audit_logger.get_log_count(status=status)
            if count > 0:
                stats['by_status'][status] = count

        # Count by resource type
        for resource_type in ['job', 'server', 's3_config', 'db_config', 'notification_channel', 'session']:
            count = audit_logger.get_log_count(resource_type=resource_type)
            if count > 0:
                stats['by_resource_type'][resource_type] = count

        return jsonify(stats)

    except Exception as e:
        logger.error(f"Error getting audit stats: {e}")
        return jsonify({'error': 'Failed to get audit statistics'}), 500


# =============================================================================
# Application Settings API Endpoints
# =============================================================================

# Common timezone list for UI selection
COMMON_TIMEZONES = [
    'UTC',
    # Americas
    'America/New_York',
    'America/Chicago',
    'America/Denver',
    'America/Los_Angeles',
    'America/Toronto',
    'America/Vancouver',
    'America/Mexico_City',
    'America/Sao_Paulo',
    'America/Buenos_Aires',
    # Europe
    'Europe/London',
    'Europe/Paris',
    'Europe/Berlin',
    'Europe/Amsterdam',
    'Europe/Rome',
    'Europe/Madrid',
    'Europe/Moscow',
    'Europe/Istanbul',
    # Africa
    'Africa/Cairo',
    'Africa/Johannesburg',
    'Africa/Lagos',
    # Middle East
    'Asia/Dubai',
    'Asia/Riyadh',
    'Asia/Jerusalem',
    # South Asia
    'Asia/Kolkata',
    'Asia/Colombo',
    'Asia/Dhaka',
    'Asia/Karachi',
    'Indian/Maldives',
    # Southeast Asia
    'Asia/Singapore',
    'Asia/Bangkok',
    'Asia/Jakarta',
    'Asia/Kuala_Lumpur',
    'Asia/Manila',
    'Asia/Ho_Chi_Minh',
    # East Asia
    'Asia/Hong_Kong',
    'Asia/Tokyo',
    'Asia/Seoul',
    'Asia/Shanghai',
    'Asia/Taipei',
    # Australia & Pacific
    'Australia/Sydney',
    'Australia/Melbourne',
    'Australia/Perth',
    'Pacific/Auckland',
    'Pacific/Fiji',
    'Pacific/Honolulu',
]


def get_app_setting(key: str, default: str = '') -> str:
    """Get an application setting from the database"""
    try:
        from .db import get_database
        db = get_database()
        result = db.fetchone('SELECT value FROM app_settings WHERE key = %s', (key,))
        return result['value'] if result else default
    except Exception as e:
        logger.error(f"Error getting app setting {key}: {e}")
        return default


def set_app_setting(key: str, value: str) -> tuple[bool, str]:
    """Set an application setting in the database. Returns (success, error_message)."""
    try:
        from .db import get_database
        db = get_database()
        # Rollback any aborted transaction first
        try:
            db.get_connection().rollback()
        except Exception:
            pass
        # Upsert the setting
        db.execute('''
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        ''', (key, value))
        db.commit()
        return True, ''
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error setting app setting {key}: {error_msg}")
        # Try to rollback on error
        try:
            from .db import get_database
            get_database().get_connection().rollback()
        except Exception:
            pass
        return False, error_msg


def get_scheduler_timezone() -> str:
    """Get the scheduler timezone from database, falling back to TZ env var or UTC"""
    db_tz = get_app_setting('timezone')
    if db_tz:
        return db_tz
    return os.environ.get('TZ', 'UTC')


@app.route('/api/settings/timezone', methods=['GET'])
@login_required
def api_get_timezone():
    """Get current timezone setting"""
    timezone = get_scheduler_timezone()
    return jsonify({
        'timezone': timezone,
        'available_timezones': COMMON_TIMEZONES
    })


@app.route('/api/settings/timezone', methods=['PUT'])
@login_required
@csrf.exempt
def api_set_timezone():
    """Set timezone for scheduler"""
    data = request.get_json()
    if not data or 'timezone' not in data:
        return jsonify({'error': 'Timezone is required'}), 400

    timezone = data['timezone']

    # Validate timezone
    try:
        import pytz
        pytz.timezone(timezone)
    except Exception:
        return jsonify({'error': f'Invalid timezone: {timezone}'}), 400

    success, error_msg = set_app_setting('timezone', timezone)
    if success:
        # Update scheduler timezone
        global scheduler
        scheduler.shutdown(wait=False)
        scheduler = BackgroundScheduler(timezone=timezone)
        scheduler.start()

        # Re-schedule all jobs with new timezone
        init_schedules()

        logger.info(f"Timezone updated to {timezone}, scheduler restarted")
        return jsonify({'success': True, 'timezone': timezone})
    else:
        return jsonify({'error': error_msg or 'Failed to save timezone setting'}), 500


# Serve React frontend
@app.route('/assets/<path:filename>')
@limiter.exempt
def serve_assets(filename):
    """Serve static assets from React build"""
    return send_from_directory(FRONTEND_DIST / 'assets', filename)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
@limiter.exempt
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
    logger.info("Initializing scheduled jobs...")
    jobs = load_jobs()
    scheduled_count = 0
    for job_id, job in jobs.items():
        if job.get('schedule_enabled'):
            schedule_job(job_id, job)
            scheduled_count += 1
            logger.info(f"Scheduled job: {job['name']} ({job_id}) - cron: {job.get('schedule_cron', '0 2 * * *')}")
    logger.info(f"Initialized {scheduled_count} scheduled jobs")


# Initialize database and migrate data on startup
def init_app():
    """Initialize the application"""
    logger.info("Initializing BackupX application...")

    # Initialize PostgreSQL database
    init_db()
    migrate_json_to_db()

    # Re-initialize scheduler with timezone from database
    reinit_scheduler_with_db_timezone()

    # Initialize audit logging
    try:
        from .audit.logger import init_audit_logger
        init_audit_logger(get_db())
        logger.info("Audit logging initialized")
    except Exception as e:
        logger.warning(f"Audit logging not available: {e}")

    # Initialize schedules
    init_schedules()

    # Reset any jobs stuck in 'running' state from a previous crash/restart
    try:
        conn = get_db_connection()
        stuck_jobs = conn.fetchall(
            "SELECT id, name FROM jobs WHERE status = 'running'"
        )
        for row in stuck_jobs:
            conn.execute(
                "UPDATE jobs SET status='failed', progress=0, progress_message='Interrupted by server restart', updated_at=? WHERE id=?",
                (utc_isoformat(), row['id'])
            )
            logger.warning(f"Reset stuck job: {row['id']} ({row['name']})")
        if stuck_jobs:
            conn.commit()
    
    except Exception as e:
        logger.warning(f"Failed to reset stuck jobs: {e}")

    logger.info("BackupX application initialized successfully")


# Run initialization on module load (for gunicorn/uwsgi)
init_app()


if __name__ == '__main__':
    # Development server only - use gunicorn in production
    debug_mode = os.environ.get('DEBUG', '').lower() in ('true', '1', 'yes')
    if debug_mode:
        logger.info("Starting in DEBUG mode - DO NOT USE IN PRODUCTION")
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)

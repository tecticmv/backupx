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
import sqlite3
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

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, g
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

def validate_environment():
    """Validate required environment variables"""
    warnings = []

    # Check SECRET_KEY
    secret_key = os.environ.get('SECRET_KEY', '')
    if not secret_key or secret_key == 'change-this-secret-key':
        warnings.append("SECRET_KEY is not set or using default value. Set a strong random key in production.")

    # Check admin credentials
    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'changeme')

    if admin_user == 'admin':
        warnings.append("ADMIN_USERNAME is using default value 'admin'. Consider changing it.")

    if admin_pass == 'changeme':
        warnings.append("ADMIN_PASSWORD is using default value. Set a strong password in production.")

    # Log warnings
    for warning in warnings:
        logger.warning(warning)

    return len(warnings) == 0


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
        # Derive a 32-byte key from SECRET_KEY using SHA-256
        secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')
        key = hashlib.sha256(secret_key.encode()).digest()
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

def get_admin_password_hash():
    """Get hashed admin password"""
    global _admin_password_hash
    if _admin_password_hash is None:
        admin_pass = os.environ.get('ADMIN_PASSWORD', 'changeme')
        _admin_password_hash = generate_password_hash(admin_pass)
    return _admin_password_hash

# Validate environment on startup
env_valid = validate_environment()
if not env_valid:
    logger.warning("Application starting with configuration warnings. Review settings for production use.")


# Initialize Flask app
FRONTEND_DIST = Path(__file__).parent.parent / 'frontend' / 'dist'
app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')

# CSRF Protection
csrf = CSRFProtect(app)

# Rate Limiting
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


# =============================================================================
# Security Headers Middleware
# =============================================================================

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    # Prevent clickjacking
    response.headers['X-Frame-Options'] = 'DENY'
    # Prevent MIME type sniffing
    response.headers['X-Content-Type-Options'] = 'nosniff'
    # Enable XSS filter
    response.headers['X-XSS-Protection'] = '1; mode=block'
    # Referrer policy
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # Permissions policy (restrict browser features)
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    # Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response

# Paths
CONFIG_DIR = Path('/app/config')
LOGS_DIR = Path('/app/logs')
DATA_DIR = Path('/app/data')
DATABASE = DATA_DIR / 'backupx.db'

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
    """Get database connection for the current request"""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


def get_db_connection():
    """Get a new database connection (for use outside request context)"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


@app.teardown_appcontext
def close_db(exception):
    """Close database connection at end of request"""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Initialize database schema"""
    conn = get_db_connection()
    conn.executescript('''
        -- Servers table
        CREATE TABLE IF NOT EXISTS servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            connection_type TEXT DEFAULT 'ssh',
            ssh_port INTEGER DEFAULT 22,
            ssh_user TEXT,
            ssh_key TEXT DEFAULT '/home/backupx/.ssh/id_rsa',
            agent_port INTEGER DEFAULT 8090,
            agent_api_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        -- S3 configurations table
        CREATE TABLE IF NOT EXISTS s3_configs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            bucket TEXT NOT NULL,
            access_key TEXT NOT NULL,
            secret_key TEXT NOT NULL,
            region TEXT DEFAULT '',
            skip_ssl_verify INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        -- Database configurations table
        CREATE TABLE IF NOT EXISTS db_configs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT DEFAULT 'mysql',
            host TEXT NOT NULL,
            port INTEGER DEFAULT 3306,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            databases TEXT DEFAULT '*',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        -- Jobs table
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            backup_type TEXT DEFAULT 'filesystem',
            server_id TEXT,
            s3_config_id TEXT,
            remote_host TEXT,
            ssh_port INTEGER DEFAULT 22,
            ssh_key TEXT,
            s3_endpoint TEXT,
            s3_bucket TEXT,
            s3_access_key TEXT,
            s3_secret_key TEXT,
            directories TEXT,
            excludes TEXT,
            database_config_id TEXT,
            restic_password TEXT,
            backup_prefix TEXT,
            schedule_enabled INTEGER DEFAULT 0,
            schedule_cron TEXT DEFAULT '0 2 * * *',
            retention_hourly INTEGER DEFAULT 24,
            retention_daily INTEGER DEFAULT 7,
            retention_weekly INTEGER DEFAULT 4,
            retention_monthly INTEGER DEFAULT 12,
            timeout INTEGER DEFAULT 7200,
            skip_ssl_verify INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_run TEXT,
            last_success TEXT,
            FOREIGN KEY (server_id) REFERENCES servers(id),
            FOREIGN KEY (s3_config_id) REFERENCES s3_configs(id),
            FOREIGN KEY (database_config_id) REFERENCES db_configs(id)
        );

        -- History table
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            job_id TEXT NOT NULL,
            job_name TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            duration REAL DEFAULT 0
        );

        -- Notification channels table
        CREATE TABLE IF NOT EXISTS notification_channels (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            config TEXT NOT NULL,
            notify_on_success INTEGER DEFAULT 1,
            notify_on_failure INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        -- Audit log table (enterprise feature)
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id TEXT,
            user_name TEXT,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            resource_name TEXT,
            changes TEXT,
            ip_address TEXT,
            user_agent TEXT,
            status TEXT DEFAULT 'success',
            error_message TEXT
        );

        -- Scheduler tables for distributed mode (enterprise feature)
        CREATE TABLE IF NOT EXISTS scheduler_lock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            leader_instance TEXT,
            acquired_at TEXT,
            heartbeat_at TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            job_id TEXT PRIMARY KEY,
            cron_expression TEXT NOT NULL,
            next_run TEXT,
            last_run TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_history_job_id ON history(job_id);
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log(resource_type, resource_id);
        CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
        CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_next_run ON scheduled_jobs(next_run);
    ''')
    conn.commit()

    # Add new columns if they don't exist (migration for existing databases)
    try:
        conn = get_db_connection()
        # Check and add connection_type column
        cursor = conn.execute("PRAGMA table_info(servers)")
        columns = [col[1] for col in cursor.fetchall()]

        if 'connection_type' not in columns:
            conn.execute("ALTER TABLE servers ADD COLUMN connection_type TEXT DEFAULT 'ssh'")
            logger.info("Added connection_type column to servers table")
        if 'agent_port' not in columns:
            conn.execute("ALTER TABLE servers ADD COLUMN agent_port INTEGER DEFAULT 8090")
            logger.info("Added agent_port column to servers table")
        if 'agent_api_key' not in columns:
            conn.execute("ALTER TABLE servers ADD COLUMN agent_api_key TEXT")
            logger.info("Added agent_api_key column to servers table")

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Migration check failed: {e}")

    conn.close()


def migrate_json_to_sqlite():
    """Migrate existing JSON data to SQLite (runs once on startup)"""
    conn = get_db_connection()

    # Check if migration is needed (check if any data exists)
    has_data = False
    for table in ['servers', 's3_configs', 'db_configs', 'jobs', 'history']:
        count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if count > 0:
            has_data = True
            break

    if has_data:
        conn.close()
        return  # Already has data, skip migration

    logger.info("Migrating JSON data to SQLite...")

    # Migrate servers
    if SERVERS_FILE.exists():
        try:
            with open(SERVERS_FILE) as f:
                servers = json.load(f)
            for s in servers:
                conn.execute('''
                    INSERT INTO servers (id, name, host, connection_type, ssh_port, ssh_user, ssh_key, agent_port, agent_api_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (s['id'], s['name'], s['host'], s.get('connection_type', 'ssh'),
                      s.get('ssh_port', 22), s.get('ssh_user'), s.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
                      s.get('agent_port', 8090), s.get('agent_api_key'),
                      s.get('created_at', utc_isoformat()), s.get('updated_at')))
            logger.info(f"  Migrated {len(servers)} servers")
        except Exception as e:
            logger.error(f"  Error migrating servers: {e}")

    # Migrate S3 configs
    if S3_CONFIGS_FILE.exists():
        try:
            with open(S3_CONFIGS_FILE) as f:
                configs = json.load(f)
            for c in configs:
                conn.execute('''
                    INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, skip_ssl_verify, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (c['id'], c['name'], c['endpoint'], c['bucket'], c['access_key'], c['secret_key'],
                      c.get('region', ''), c.get('skip_ssl_verify', 0), c.get('created_at', utc_isoformat()), c.get('updated_at')))
            logger.info(f"  Migrated {len(configs)} S3 configs")
        except Exception as e:
            logger.error(f"  Error migrating S3 configs: {e}")

    # Migrate database configs
    if DB_CONFIGS_FILE.exists():
        try:
            with open(DB_CONFIGS_FILE) as f:
                configs = json.load(f)
            for c in configs:
                conn.execute('''
                    INSERT INTO db_configs (id, name, type, host, port, username, password, databases, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (c['id'], c['name'], c.get('type', 'mysql'), c['host'], c.get('port', 3306),
                      c['username'], c['password'], c.get('databases', '*'),
                      c.get('created_at', utc_isoformat()), c.get('updated_at')))
            logger.info(f"  Migrated {len(configs)} database configs")
        except Exception as e:
            logger.error(f"  Error migrating database configs: {e}")

    # Migrate jobs
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE) as f:
                jobs = json.load(f)
            for job_id, j in jobs.items():
                conn.execute('''
                    INSERT INTO jobs (id, name, backup_type, server_id, s3_config_id, remote_host, ssh_port, ssh_key,
                        s3_endpoint, s3_bucket, s3_access_key, s3_secret_key, directories, excludes, database_config_id,
                        restic_password, backup_prefix, schedule_enabled, schedule_cron, retention_hourly, retention_daily,
                        retention_weekly, retention_monthly, timeout, status, created_at, updated_at, last_run, last_success)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (job_id, j['name'], j.get('backup_type', 'filesystem'), j.get('server_id'), j.get('s3_config_id'),
                      j.get('remote_host'), j.get('ssh_port', 22), j.get('ssh_key'),
                      j.get('s3_endpoint'), j.get('s3_bucket'), j.get('s3_access_key'), j.get('s3_secret_key'),
                      json.dumps(j.get('directories', [])), json.dumps(j.get('excludes', [])), j.get('database_config_id'),
                      j.get('restic_password'), j.get('backup_prefix'), 1 if j.get('schedule_enabled') else 0,
                      j.get('schedule_cron', '0 2 * * *'), j.get('retention_hourly', 24), j.get('retention_daily', 7),
                      j.get('retention_weekly', 4), j.get('retention_monthly', 12), j.get('timeout', 7200),
                      j.get('status', 'pending'), j.get('created_at', utc_isoformat()), j.get('updated_at'),
                      j.get('last_run'), j.get('last_success')))
            logger.info(f"  Migrated {len(jobs)} jobs")
        except Exception as e:
            logger.error(f"  Error migrating jobs: {e}")

    # Migrate history
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
            for h in history:
                conn.execute('''
                    INSERT INTO history (timestamp, job_id, job_name, status, message, duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (h['timestamp'], h['job_id'], h['job_name'], h['status'], h.get('message', ''), h.get('duration', 0)))
            logger.info(f"  Migrated {len(history)} history entries")
        except Exception as e:
            logger.error(f"  Error migrating history: {e}")

    conn.commit()
    conn.close()
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
    rows = conn.execute('SELECT * FROM jobs').fetchall()
    conn.close()

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
    row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    conn.close()

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
    exists = conn.execute('SELECT 1 FROM jobs WHERE id = ?', (job_id,)).fetchone()

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
              encrypted_restic_password, job.get('backup_prefix'), 1 if job.get('schedule_enabled') else 0,
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
              encrypted_restic_password, job.get('backup_prefix'), 1 if job.get('schedule_enabled') else 0,
              job.get('schedule_cron', '0 2 * * *'), job.get('retention_hourly', 24), job.get('retention_daily', 7),
              job.get('retention_weekly', 4), job.get('retention_monthly', 12), job.get('timeout', 7200),
              job.get('status', 'pending'), job.get('created_at', utc_isoformat()), job.get('updated_at'),
              job.get('last_run'), job.get('last_success')))

    conn.commit()
    conn.close()


def delete_job_from_db(job_id):
    """Delete a job from database"""
    conn = get_db_connection()
    conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
    conn.commit()
    conn.close()


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
    conn.close()


# --- History ---

def load_history():
    """Load backup history from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM history ORDER BY timestamp DESC LIMIT 100').fetchall()
    conn.close()
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
    conn.close()


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
    rows = conn.execute('SELECT * FROM s3_configs').fetchall()
    conn.close()
    return [_decrypt_s3_config(dict(row)) for row in rows]


def get_s3_config(config_id):
    """Get a single S3 config by ID"""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM s3_configs WHERE id = ?', (config_id,)).fetchone()
    conn.close()
    return _decrypt_s3_config(dict(row)) if row else None


def create_s3_config(config):
    """Create a new S3 config"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, skip_ssl_verify, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (config['id'], config['name'], config['endpoint'], config['bucket'],
          encrypt_credential(config['access_key']), encrypt_credential(config['secret_key']),
          config.get('region', ''), 1 if config.get('skip_ssl_verify') else 0,
          config.get('created_at', utc_isoformat()), config.get('updated_at')))
    conn.commit()
    conn.close()


def update_s3_config(config_id, config):
    """Update an S3 config"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE s3_configs SET name=?, endpoint=?, bucket=?, access_key=?, secret_key=?, region=?, skip_ssl_verify=?, updated_at=?
        WHERE id=?
    ''', (config['name'], config['endpoint'], config['bucket'],
          encrypt_credential(config['access_key']), encrypt_credential(config['secret_key']),
          config.get('region', ''), 1 if config.get('skip_ssl_verify') else 0,
          utc_isoformat(), config_id))
    conn.commit()
    conn.close()


def delete_s3_config(config_id):
    """Delete an S3 config"""
    conn = get_db_connection()
    conn.execute('DELETE FROM s3_configs WHERE id = ?', (config_id,))
    conn.commit()
    conn.close()


# --- Servers ---

def _decrypt_server(server):
    """Decrypt sensitive fields in server config"""
    if server:
        server['agent_api_key'] = decrypt_credential(server.get('agent_api_key', '') or '')
    return server


def load_servers():
    """Load servers from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM servers').fetchall()
    conn.close()
    return [_decrypt_server(dict(row)) for row in rows]


def get_server(server_id):
    """Get a single server by ID"""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM servers WHERE id = ?', (server_id,)).fetchone()
    conn.close()
    return _decrypt_server(dict(row)) if row else None


def create_server(server):
    """Create a new server"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO servers (id, name, host, connection_type, ssh_port, ssh_user, ssh_key, agent_port, agent_api_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (server['id'], server['name'], server['host'], server.get('connection_type', 'ssh'),
          server.get('ssh_port', 22), server.get('ssh_user'), server.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
          server.get('agent_port', 8090), encrypt_credential(server.get('agent_api_key', '') or ''),
          server.get('created_at', utc_isoformat()), server.get('updated_at')))
    conn.commit()
    conn.close()


def update_server_in_db(server_id, server):
    """Update a server"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE servers SET name=?, host=?, connection_type=?, ssh_port=?, ssh_user=?, ssh_key=?, agent_port=?, agent_api_key=?, updated_at=?
        WHERE id=?
    ''', (server['name'], server['host'], server.get('connection_type', 'ssh'),
          server.get('ssh_port', 22), server.get('ssh_user'), server.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
          server.get('agent_port', 8090), encrypt_credential(server.get('agent_api_key', '') or ''),
          utc_isoformat(), server_id))
    conn.commit()
    conn.close()


def delete_server_from_db(server_id):
    """Delete a server"""
    conn = get_db_connection()
    conn.execute('DELETE FROM servers WHERE id = ?', (server_id,))
    conn.commit()
    conn.close()


# --- Database Configs ---

def _decrypt_db_config(config):
    """Decrypt sensitive fields in database config"""
    if config:
        config['password'] = decrypt_credential(config.get('password', ''))
    return config


def load_db_configs():
    """Load database configurations from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM db_configs').fetchall()
    conn.close()
    return [_decrypt_db_config(dict(row)) for row in rows]


def get_db_config(config_id):
    """Get a single database config by ID"""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM db_configs WHERE id = ?', (config_id,)).fetchone()
    conn.close()
    return _decrypt_db_config(dict(row)) if row else None


def create_db_config(config):
    """Create a new database config"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO db_configs (id, name, type, host, port, username, password, databases, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (config['id'], config['name'], config.get('type', 'mysql'), config['host'], config.get('port', 3306),
          config['username'], encrypt_credential(config['password']), config.get('databases', '*'),
          config.get('created_at', utc_isoformat()), config.get('updated_at')))
    conn.commit()
    conn.close()


def update_db_config_in_db(config_id, config):
    """Update a database config"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE db_configs SET name=?, type=?, host=?, port=?, username=?, password=?, databases=?, updated_at=?
        WHERE id=?
    ''', (config['name'], config.get('type', 'mysql'), config['host'], config.get('port', 3306),
          config['username'], encrypt_credential(config['password']), config.get('databases', '*'),
          utc_isoformat(), config_id))
    conn.commit()
    conn.close()


def delete_db_config_from_db(config_id):
    """Delete a database config"""
    conn = get_db_connection()
    conn.execute('DELETE FROM db_configs WHERE id = ?', (config_id,))
    conn.commit()
    conn.close()


# --- Notification Channels ---

def load_notification_channels():
    """Load all notification channels from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM notification_channels').fetchall()
    conn.close()
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
    row = conn.execute('SELECT * FROM notification_channels WHERE id = ?', (channel_id,)).fetchone()
    conn.close()
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
    ''', (channel['id'], channel['name'], channel['type'], 1 if channel.get('enabled', True) else 0,
          json.dumps(channel.get('config', {})), 1 if channel.get('notify_on_success', True) else 0,
          1 if channel.get('notify_on_failure', True) else 0, channel.get('created_at', utc_isoformat()),
          channel.get('updated_at')))
    conn.commit()
    conn.close()


def update_notification_channel(channel_id, channel):
    """Update a notification channel"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE notification_channels SET name=?, type=?, enabled=?, config=?, notify_on_success=?, notify_on_failure=?, updated_at=?
        WHERE id=?
    ''', (channel['name'], channel['type'], 1 if channel.get('enabled', True) else 0,
          json.dumps(channel.get('config', {})), 1 if channel.get('notify_on_success', True) else 0,
          1 if channel.get('notify_on_failure', True) else 0, utc_isoformat(), channel_id))
    conn.commit()
    conn.close()


def delete_notification_channel(channel_id):
    """Delete a notification channel"""
    conn = get_db_connection()
    conn.execute('DELETE FROM notification_channels WHERE id = ?', (channel_id,))
    conn.commit()
    conn.close()


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
    text = f"""<b>{emoji} Backup Job: {job_name}</b>

<b>Status:</b> {status.upper()}
<b>Duration:</b> {format_duration(duration)}

<pre>{message[:3000] if message else 'No additional details'}</pre>"""

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
    except (URLError, HTTPError) as e:
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

    # Get server to determine connection type
    server_id = job.get('server_id')
    server = get_server(server_id) if server_id else None

    backup_type = job.get('backup_type', 'filesystem')
    connection_type = server.get('connection_type', 'ssh') if server else 'ssh'

    if connection_type == 'agent' and server:
        # Use agent-based backup
        if backup_type == 'database':
            return run_agent_database_backup(job_id, job, server)
        else:
            return run_agent_filesystem_backup(job_id, job, server)
    else:
        # Use SSH-based backup
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


def run_filesystem_backup(job_id, job):
    """Execute a filesystem backup job"""
    start_time = utc_now()
    logger.info(f"Starting filesystem backup job: {job_id} ({job['name']})")

    # Update job status
    update_job_status(job_id, 'running', last_run=start_time.isoformat())

    try:
        # Build exclude args with proper escaping
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
        ssh_cmd = [
            'ssh', '-i', job.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
            '-p', str(job.get('ssh_port', 22)),
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=30',
            job['remote_host']
        ]

        # Build remote command with proper escaping
        insecure_flag = '--insecure-tls' if job.get('skip_ssl_verify') else ''
        remote_cmd = f"""
export AWS_ACCESS_KEY_ID={s3_access_key}
export AWS_SECRET_ACCESS_KEY={s3_secret_key}
export RESTIC_PASSWORD={restic_password}
export RESTIC_REPOSITORY="s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"
restic backup --compression auto --tag automated {insecure_flag} {' '.join(exclude_args)} {directories}
"""

        # Execute
        result = subprocess.run(
            ssh_cmd + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=job.get('timeout', 7200)  # 2 hour default timeout
        )

        duration = (utc_now() - start_time).total_seconds()

        if result.returncode == 0:
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=utc_isoformat())
            add_history(job_id, job['name'], 'success', 'Backup completed successfully', duration)
            send_notification(job_id, job['name'], 'success', 'Backup completed successfully', duration)
            logger.info(f"Filesystem backup completed successfully: {job_id} (duration: {duration:.1f}s)")
            return True, "Backup completed successfully"
        else:
            error_msg = sanitize_error_message(result.stderr)
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

    try:
        # Get database config
        db_config_id = job.get('database_config_id')
        if not db_config_id:
            raise Exception("Database configuration not specified")

        db_configs = load_db_configs()
        db_config = next((c for c in db_configs if c['id'] == db_config_id), None)
        if not db_config:
            raise Exception("Database configuration not found")

        # Build SSH command to run on remote server
        ssh_cmd = [
            'ssh', '-i', job.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
            '-p', str(job.get('ssh_port', 22)),
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=30',
            job['remote_host']
        ]

        # Get database list with proper escaping
        databases = db_config.get('databases', '*')
        if databases == '*':
            db_flag = '--all-databases'
        else:
            # Multiple databases separated by comma or single db - escape each
            db_list = [shlex.quote(db.strip()) for db in databases.split(',') if db.strip()]
            db_flag = '--databases ' + ' '.join(db_list)

        # Generate backup filename with timestamp
        timestamp = utc_now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"mysql_backup_{timestamp}.sql.gz"

        # Escape all values for shell
        s3_access_key = shlex.quote(job['s3_access_key'])
        s3_secret_key = shlex.quote(job['s3_secret_key'])
        restic_password = shlex.quote(job['restic_password'])
        db_host = shlex.quote(db_config['host'])
        db_port = int(db_config.get('port', 3306))
        db_user = shlex.quote(db_config['username'])
        db_pass = shlex.quote(db_config['password'])

        # Build remote command with proper escaping
        insecure_flag = '--insecure-tls' if job.get('skip_ssl_verify') else ''
        remote_cmd = f"""
export AWS_ACCESS_KEY_ID={s3_access_key}
export AWS_SECRET_ACCESS_KEY={s3_secret_key}
export RESTIC_PASSWORD={restic_password}
export RESTIC_REPOSITORY="s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

# Create temp directory for backup
BACKUP_DIR=$(mktemp -d)
BACKUP_FILE="$BACKUP_DIR/{backup_filename}"

# Dump MySQL database
mysqldump -h {db_host} -P {db_port} -u {db_user} -p{db_pass} {db_flag} --single-transaction --routines --triggers | gzip > "$BACKUP_FILE"

if [ $? -ne 0 ]; then
    echo "mysqldump failed"
    rm -rf "$BACKUP_DIR"
    exit 1
fi

# Backup to restic repository
restic backup --compression auto --tag automated --tag mysql-backup {insecure_flag} "$BACKUP_FILE"
RESTIC_EXIT=$?

# Cleanup
rm -rf "$BACKUP_DIR"

exit $RESTIC_EXIT
"""

        # Execute
        result = subprocess.run(
            ssh_cmd + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=job.get('timeout', 7200)  # 2 hour default timeout
        )

        duration = (utc_now() - start_time).total_seconds()

        if result.returncode == 0:
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=utc_isoformat())
            message = f'MySQL backup completed successfully ({databases})'
            add_history(job_id, job['name'], 'success', message, duration)
            send_notification(job_id, job['name'], 'success', message, duration)
            logger.info(f"Database backup completed successfully: {job_id} (duration: {duration:.1f}s)")
            return True, "Database backup completed successfully"
        else:
            error_msg = sanitize_error_message(result.stderr)
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


def run_agent_filesystem_backup(job_id, job, server):
    """Execute a filesystem backup job via agent"""
    start_time = utc_now()
    logger.info(f"Starting agent-based filesystem backup job: {job_id} ({job['name']})")

    # Update job status
    update_job_status(job_id, 'running', last_run=start_time.isoformat())

    try:
        agent_url = f"http://{server['host']}:{server.get('agent_port', 8090)}/backup/filesystem"

        # Prepare request payload
        payload = {
            's3_endpoint': job['s3_endpoint'],
            's3_bucket': job['s3_bucket'],
            's3_access_key': job['s3_access_key'],
            's3_secret_key': job['s3_secret_key'],
            'restic_password': job['restic_password'],
            'backup_prefix': job.get('backup_prefix', job_id),
            'directories': job.get('directories', []),
            'excludes': job.get('excludes', []),
            'skip_ssl_verify': job.get('skip_ssl_verify', False)
        }

        # Log payload details (without secrets) for debugging
        logger.debug(f"Agent backup payload: directories={payload['directories']}, "
                    f"s3_endpoint={payload['s3_endpoint']}, s3_bucket={payload['s3_bucket']}, "
                    f"backup_prefix={payload['backup_prefix']}, skip_ssl_verify={payload['skip_ssl_verify']}")

        data = json.dumps(payload).encode('utf-8')
        req = Request(agent_url, data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('X-API-Key', server['agent_api_key'])

        # Make request with timeout
        timeout = job.get('timeout', 7200)
        with urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))

        duration = (utc_now() - start_time).total_seconds()

        if result.get('success'):
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=utc_isoformat())
            add_history(job_id, job['name'], 'success', 'Backup completed successfully via agent', duration)
            send_notification(job_id, job['name'], 'success', 'Backup completed successfully via agent', duration)
            logger.info(f"Agent filesystem backup completed successfully: {job_id} (duration: {duration:.1f}s)")
            return True, "Backup completed successfully"
        else:
            error_msg = sanitize_error_message(result.get('error', 'Unknown error'))
            update_job_status(job_id, 'failed')
            add_history(job_id, job['name'], 'failed', error_msg, duration)
            send_notification(job_id, job['name'], 'failed', error_msg, duration)
            logger.error(f"Agent filesystem backup failed: {job_id} - {error_msg[:200]}")
            return False, error_msg

    except (URLError, HTTPError) as e:
        duration = (utc_now() - start_time).total_seconds()
        if hasattr(e, 'code') and e.code == 401:
            error_msg = "Agent authentication failed - check API key"
        elif hasattr(e, 'code') and hasattr(e, 'read'):
            # Read the error response from agent
            try:
                error_response = json.loads(e.read().decode('utf-8'))
                error_msg = error_response.get('error', str(e))
            except:
                error_msg = sanitize_error_message(str(e))
        else:
            error_msg = sanitize_error_message(str(e))
        update_job_status(job_id, 'error')
        add_history(job_id, job['name'], 'error', error_msg, duration)
        send_notification(job_id, job['name'], 'error', error_msg, duration)
        logger.error(f"Agent filesystem backup error: {job_id} - {error_msg}")
        return False, error_msg
    except Exception as e:
        duration = (utc_now() - start_time).total_seconds()
        error_msg = sanitize_error_message(str(e))
        update_job_status(job_id, 'error')
        add_history(job_id, job['name'], 'error', error_msg, duration)
        send_notification(job_id, job['name'], 'error', error_msg, duration)
        logger.exception(f"Agent filesystem backup error: {job_id}")
        return False, error_msg


def run_agent_database_backup(job_id, job, server):
    """Execute a MySQL database backup job via agent"""
    start_time = utc_now()
    logger.info(f"Starting agent-based database backup job: {job_id} ({job['name']})")

    # Update job status
    update_job_status(job_id, 'running', last_run=start_time.isoformat())

    try:
        # Get database config
        db_config_id = job.get('database_config_id')
        if not db_config_id:
            raise Exception("Database configuration not specified")

        db_config = get_db_config(db_config_id)
        if not db_config:
            raise Exception("Database configuration not found")

        agent_url = f"http://{server['host']}:{server.get('agent_port', 8090)}/backup/database"

        # Prepare request payload
        payload = {
            's3_endpoint': job['s3_endpoint'],
            's3_bucket': job['s3_bucket'],
            's3_access_key': job['s3_access_key'],
            's3_secret_key': job['s3_secret_key'],
            'restic_password': job['restic_password'],
            'backup_prefix': job.get('backup_prefix', job_id),
            'db_type': db_config.get('type', 'mysql'),
            'db_host': db_config['host'],
            'db_port': db_config.get('port', 3306),
            'db_user': db_config['username'],
            'db_password': db_config['password'],
            'databases': db_config.get('databases', '*'),
            'skip_ssl_verify': job.get('skip_ssl_verify', False)
        }

        data = json.dumps(payload).encode('utf-8')
        req = Request(agent_url, data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('X-API-Key', server['agent_api_key'])

        # Make request with timeout
        timeout = job.get('timeout', 7200)
        with urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))

        duration = (utc_now() - start_time).total_seconds()

        if result.get('success'):
            databases = db_config.get('databases', '*')
            message = f'MySQL backup completed successfully via agent ({databases})'
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=utc_isoformat())
            add_history(job_id, job['name'], 'success', message, duration)
            send_notification(job_id, job['name'], 'success', message, duration)
            logger.info(f"Agent database backup completed successfully: {job_id} (duration: {duration:.1f}s)")
            return True, "Database backup completed successfully"
        else:
            error_msg = sanitize_error_message(result.get('error', 'Unknown error'))
            update_job_status(job_id, 'failed')
            add_history(job_id, job['name'], 'failed', error_msg, duration)
            send_notification(job_id, job['name'], 'failed', error_msg, duration)
            logger.error(f"Agent database backup failed: {job_id} - {error_msg[:200]}")
            return False, error_msg

    except (URLError, HTTPError) as e:
        duration = (utc_now() - start_time).total_seconds()
        if hasattr(e, 'code') and e.code == 401:
            error_msg = "Agent authentication failed - check API key"
        else:
            error_msg = sanitize_error_message(str(e))
        update_job_status(job_id, 'error')
        add_history(job_id, job['name'], 'error', error_msg, duration)
        send_notification(job_id, job['name'], 'error', error_msg, duration)
        logger.error(f"Agent database backup error: {job_id} - {error_msg}")
        return False, error_msg
    except Exception as e:
        duration = (utc_now() - start_time).total_seconds()
        error_msg = sanitize_error_message(str(e))
        update_job_status(job_id, 'error')
        add_history(job_id, job['name'], 'error', error_msg, duration)
        send_notification(job_id, job['name'], 'error', error_msg, duration)
        logger.exception(f"Agent database backup error: {job_id}")
        return False, error_msg


def get_snapshots(job, server=None):
    """Get list of snapshots for a job"""
    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    # If using agent, call agent's /snapshots endpoint
    if server and server.get('connection_type') == 'agent':
        try:
            agent_url = f"http://{server['host']}:{server.get('agent_port', 8090)}/snapshots"

            payload = {
                's3_endpoint': job['s3_endpoint'],
                's3_bucket': job['s3_bucket'],
                's3_access_key': job['s3_access_key'],
                's3_secret_key': job['s3_secret_key'],
                'restic_password': job['restic_password'],
                'backup_prefix': job.get('backup_prefix', job.get('id', '')),
                'skip_ssl_verify': skip_ssl_verify
            }

            data = json.dumps(payload).encode('utf-8')
            req = Request(agent_url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-API-Key', server['agent_api_key'])

            with urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode('utf-8'))

            if result.get('success'):
                snapshots = result.get('snapshots', [])
                # Sort by time, newest first
                snapshots.sort(key=lambda x: x.get('time', ''), reverse=True)
                return snapshots
            return []
        except Exception as e:
            logger.error(f"Failed to get snapshots via agent: {e}")
            return []

    # Local restic command
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
            return snapshots
        return []
    except:
        return []


def get_repo_stats(job, server=None):
    """Get repository statistics"""
    # Get skip_ssl_verify from S3 config
    s3_config_id = job.get('s3_config_id')
    skip_ssl_verify = False
    if s3_config_id:
        s3_config = get_s3_config(s3_config_id)
        if s3_config:
            skip_ssl_verify = s3_config.get('skip_ssl_verify', False)

    # If using agent, call agent's /stats endpoint
    if server and server.get('connection_type') == 'agent':
        try:
            agent_url = f"http://{server['host']}:{server.get('agent_port', 8090)}/stats"

            payload = {
                's3_endpoint': job['s3_endpoint'],
                's3_bucket': job['s3_bucket'],
                's3_access_key': job['s3_access_key'],
                's3_secret_key': job['s3_secret_key'],
                'restic_password': job['restic_password'],
                'backup_prefix': job.get('backup_prefix', job.get('id', '')),
                'skip_ssl_verify': skip_ssl_verify
            }

            data = json.dumps(payload).encode('utf-8')
            req = Request(agent_url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-API-Key', server['agent_api_key'])

            with urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode('utf-8'))

            if result.get('success'):
                return result.get('stats')
            return None
        except Exception as e:
            logger.error(f"Failed to get stats via agent: {e}")
            return None

    # Local restic command
    try:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = job['s3_access_key']
        env['AWS_SECRET_ACCESS_KEY'] = job['s3_secret_key']
        env['RESTIC_PASSWORD'] = job['restic_password']
        env['RESTIC_REPOSITORY'] = f"s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"

        cmd = ['restic', 'stats', '--json']
        if skip_ssl_verify:
            cmd.append('--insecure-tls')

        result = subprocess.run(
            cmd,
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


# API Routes
@app.route('/api/jobs')
@login_required
def api_jobs():
    jobs = load_jobs()
    return jsonify(jobs)


@app.route('/api/jobs/<job_id>/status')
@login_required
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

    # Check if job already exists
    if get_job(job_id):
        return jsonify({'error': 'Job ID already exists'}), 400

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
    except:
        pass

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

    # Run in background
    thread = threading.Thread(target=run_backup, args=[job_id])
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

    # Get server to determine connection type
    server_id = job.get('server_id')
    server = get_server(server_id) if server_id else None

    if server and server.get('connection_type') == 'agent':
        # Use agent to initialize
        try:
            agent_url = f"http://{server['host']}:{server.get('agent_port', 8090)}/init"

            payload = {
                's3_endpoint': job['s3_endpoint'],
                's3_bucket': job['s3_bucket'],
                's3_access_key': job['s3_access_key'],
                's3_secret_key': job['s3_secret_key'],
                'restic_password': job['restic_password'],
                'backup_prefix': job.get('backup_prefix', job_id),
                'skip_ssl_verify': job.get('skip_ssl_verify', False)
            }

            data = json.dumps(payload).encode('utf-8')
            req = Request(agent_url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-API-Key', server['agent_api_key'])

            with urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode('utf-8'))

            if result.get('success'):
                return jsonify({'success': True, 'message': result.get('message', 'Repository initialized')})
            else:
                return jsonify({'success': False, 'error': result.get('error', 'Failed to initialize repository')}), 400

        except (URLError, HTTPError) as e:
            if hasattr(e, 'code') and hasattr(e, 'read'):
                try:
                    error_response = json.loads(e.read().decode('utf-8'))
                    error_msg = error_response.get('error', str(e))
                except:
                    error_msg = str(e)
            else:
                error_msg = str(e)
            return jsonify({'success': False, 'error': error_msg}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    else:
        # SSH-based init
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
def api_job_snapshots(job_id):
    """Get snapshots for a job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    # Get server for agent-based jobs
    server_id = job.get('server_id')
    server = get_server(server_id) if server_id else None

    snapshots = get_snapshots(job, server)
    stats = get_repo_stats(job, server)

    return jsonify({
        'snapshots': snapshots,
        'stats': stats
    })


@app.route('/api/jobs/<job_id>/snapshots/<snapshot_id>/files')
@login_required
def api_snapshot_files(job_id, snapshot_id):
    """List files in a snapshot"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    path = request.args.get('path', '/')

    # Sanitize path
    if '..' in path:
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

    # Sanitize path
    if '..' in file_path:
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

        # Create the restic restore command - restore specific path to target
        restic_cmd = f"restic restore {snapshot_id} --target {target_path} --include {source_path}"
        if skip_ssl_verify:
            restic_cmd += " --insecure-tls"

        # Environment variables for restic
        env_exports = f"""
export AWS_ACCESS_KEY_ID='{job['s3_access_key']}'
export AWS_SECRET_ACCESS_KEY='{job['s3_secret_key']}'
export RESTIC_PASSWORD='{job['restic_password']}'
export RESTIC_REPOSITORY='{repo}'
"""
        full_cmd = env_exports + restic_cmd

        # Check if this is an agent-based server
        if server.get('agent_url'):
            # Use agent to run the restore
            agent_url = server['agent_url'].rstrip('/')
            try:
                agent_response = requests.post(
                    f"{agent_url}/execute",
                    json={'command': full_cmd},
                    timeout=600,
                    verify=not server.get('skip_ssl_verify', False)
                )
                if agent_response.status_code == 200:
                    result = agent_response.json()
                    if result.get('exit_code', 1) != 0:
                        return jsonify({'error': result.get('stderr', 'Restore failed')}), 400
                    return jsonify({
                        'success': True,
                        'message': f"Restored {source_path} to {target_path}",
                        'output': result.get('stdout', '')
                    })
                else:
                    return jsonify({'error': 'Agent request failed'}), 400
            except requests.exceptions.RequestException as e:
                return jsonify({'error': f'Agent connection failed: {str(e)}'}), 400
        else:
            # Use SSH to run the restore
            ssh_host = server.get('host')
            ssh_port = server.get('ssh_port') or 22
            ssh_key = server.get('ssh_key')

            if not ssh_host or not ssh_key:
                return jsonify({'error': 'Server SSH configuration incomplete'}), 400

            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.key', delete=False) as key_file:
                key_file.write(ssh_key)
                key_file_path = key_file.name

            try:
                os.chmod(key_file_path, 0o600)

                ssh_cmd = [
                    'ssh', '-o', 'StrictHostKeyChecking=no',
                    '-o', 'BatchMode=yes',
                    '-i', key_file_path,
                    '-p', str(ssh_port),
                    f'root@{ssh_host}',
                    full_cmd
                ]

                result = subprocess.run(
                    ssh_cmd,
                    capture_output=True,
                    text=True,
                    timeout=600
                )

                if result.returncode != 0:
                    return jsonify({'error': result.stderr or 'Restore failed'}), 400

                return jsonify({
                    'success': True,
                    'message': f"Restored {source_path} to {target_path}",
                    'output': result.stdout
                })

            finally:
                os.unlink(key_file_path)

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Restore timed out'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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

    # Sanitize path
    if '..' in file_path:
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
def api_create_s3_config_route():
    """Create a new S3 configuration"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required_fields = ['name', 'endpoint', 'bucket', 'access_key', 'secret_key']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    new_config = {
        'id': generate_id(),
        'name': data['name'],
        'endpoint': data['endpoint'],
        'bucket': data['bucket'],
        'access_key': data['access_key'],
        'secret_key': data['secret_key'],
        'region': data.get('region', ''),
        'skip_ssl_verify': data.get('skip_ssl_verify', False),
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
def api_get_servers():
    """Get all servers"""
    servers = load_servers()
    # Mask agent API keys in response
    safe_servers = []
    for server in servers:
        safe_server = {**server}
        if safe_server.get('agent_api_key'):
            safe_server['agent_api_key'] = '********'
        safe_servers.append(safe_server)
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

    connection_type = data.get('connection_type', 'ssh')

    if connection_type == 'ssh':
        # SSH-specific validation
        if not data.get('ssh_user'):
            return jsonify({'error': 'ssh_user is required for SSH connections'}), 400

        ssh_port = int(data.get('ssh_port', 22))
        if not validate_port(ssh_port):
            return jsonify({'error': 'Invalid SSH port (must be 1-65535)'}), 400

        ssh_key = data.get('ssh_key', '/home/backupx/.ssh/id_rsa')
        if not validate_path(ssh_key):
            return jsonify({'error': 'Invalid SSH key path'}), 400

        new_server = {
            'id': generate_id(),
            'name': data['name'],
            'host': data['host'],
            'connection_type': 'ssh',
            'ssh_port': ssh_port,
            'ssh_user': data['ssh_user'],
            'ssh_key': ssh_key,
            'agent_port': None,
            'agent_api_key': None,
            'created_at': utc_isoformat(),
            'updated_at': utc_isoformat()
        }
    elif connection_type == 'agent':
        # Agent-specific validation
        if not data.get('agent_api_key'):
            return jsonify({'error': 'agent_api_key is required for agent connections'}), 400

        agent_port = int(data.get('agent_port', 8090))
        if not validate_port(agent_port):
            return jsonify({'error': 'Invalid agent port (must be 1-65535)'}), 400

        new_server = {
            'id': generate_id(),
            'name': data['name'],
            'host': data['host'],
            'connection_type': 'agent',
            'ssh_port': None,
            'ssh_user': None,
            'ssh_key': None,
            'agent_port': agent_port,
            'agent_api_key': data['agent_api_key'],
            'created_at': utc_isoformat(),
            'updated_at': utc_isoformat()
        }
    else:
        return jsonify({'error': 'Invalid connection_type. Must be "ssh" or "agent"'}), 400

    create_server(new_server)

    # Mask API key in response
    response_server = {**new_server}
    if response_server.get('agent_api_key'):
        response_server['agent_api_key'] = '********'

    return jsonify(response_server), 201


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

    # Update common fields
    server['name'] = data.get('name', server['name'])
    server['host'] = data.get('host', server['host'])
    server['connection_type'] = data.get('connection_type', server.get('connection_type', 'ssh'))

    # Update type-specific fields
    if server['connection_type'] == 'ssh':
        server['ssh_port'] = int(data.get('ssh_port', server.get('ssh_port', 22)))
        server['ssh_user'] = data.get('ssh_user', server.get('ssh_user'))
        server['ssh_key'] = data.get('ssh_key', server.get('ssh_key', '/home/backupx/.ssh/id_rsa'))
        server['agent_port'] = None
        server['agent_api_key'] = None
    elif server['connection_type'] == 'agent':
        server['agent_port'] = int(data.get('agent_port', server.get('agent_port', 8090)))
        # Only update API key if provided and not masked
        if data.get('agent_api_key') and data.get('agent_api_key') != '********':
            server['agent_api_key'] = data['agent_api_key']
        server['ssh_port'] = None
        server['ssh_user'] = None
        server['ssh_key'] = None

    update_server_in_db(server_id, server)

    # Mask API key in response
    response_server = {**server}
    if response_server.get('agent_api_key'):
        response_server['agent_api_key'] = '********'

    return jsonify(response_server)


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
    """Test connection to server (SSH or Agent)"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    connection_type = data.get('connection_type', 'ssh')
    host = data.get('host', '')

    if not host:
        return jsonify({'error': 'Host is required'}), 400

    if connection_type == 'ssh':
        # Test SSH connection
        ssh_port = int(data.get('ssh_port', 22))
        ssh_user = data.get('ssh_user', '')
        ssh_key = data.get('ssh_key', '/home/backupx/.ssh/id_rsa')

        if not ssh_user:
            return jsonify({'error': 'SSH user is required'}), 400

        try:
            ssh_cmd = [
                'ssh', '-i', ssh_key,
                '-p', str(ssh_port),
                '-o', 'StrictHostKeyChecking=accept-new',
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
                return jsonify({'success': True, 'message': 'SSH connection successful'})
            else:
                return jsonify({'error': result.stderr or 'SSH connection failed'}), 400

        except subprocess.TimeoutExpired:
            return jsonify({'error': 'SSH connection timed out'}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    elif connection_type == 'agent':
        # Test Agent connection
        agent_port = int(data.get('agent_port', 8090))
        agent_api_key = data.get('agent_api_key', '')

        if not agent_api_key:
            return jsonify({'error': 'Agent API key is required'}), 400

        try:
            # Call the agent's /health endpoint first (no auth required)
            health_url = f'http://{host}:{agent_port}/health'
            health_req = Request(health_url, method='GET')
            health_req.add_header('Content-Type', 'application/json')

            try:
                with urlopen(health_req, timeout=10) as response:
                    if response.status != 200:
                        return jsonify({'error': 'Agent health check failed'}), 400
            except (URLError, HTTPError) as e:
                return jsonify({'error': f'Cannot reach agent: {str(e)}'}), 400

            # Now test with API key via /info endpoint
            info_url = f'http://{host}:{agent_port}/info'
            info_req = Request(info_url, method='GET')
            info_req.add_header('Content-Type', 'application/json')
            info_req.add_header('X-API-Key', agent_api_key)

            try:
                with urlopen(info_req, timeout=10) as response:
                    if response.status == 200:
                        info_data = json.loads(response.read().decode('utf-8'))
                        agent_name = info_data.get('agent_name', 'Unknown')
                        return jsonify({
                            'success': True,
                            'message': f'Agent connection successful (Agent: {agent_name})'
                        })
                    else:
                        return jsonify({'error': 'Agent authentication failed'}), 400
            except HTTPError as e:
                if e.code == 401:
                    return jsonify({'error': 'Invalid API key'}), 400
                return jsonify({'error': f'Agent request failed: {str(e)}'}), 400

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    else:
        return jsonify({'error': 'Invalid connection_type'}), 400


@app.route('/api/servers/<server_id>/test', methods=['POST'])
@login_required
@csrf.exempt
def api_test_server_connection_by_id(server_id):
    """Test connection to a saved server by ID"""
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    connection_type = server.get('connection_type', 'ssh')
    host = server.get('host', '')

    if connection_type == 'ssh':
        # Test SSH connection
        ssh_port = int(server.get('ssh_port') or 22)
        ssh_user = server.get('ssh_user') or ''
        ssh_key = server.get('ssh_key') or '/home/backupx/.ssh/id_rsa'

        try:
            ssh_cmd = [
                'ssh', '-i', ssh_key,
                '-p', str(ssh_port),
                '-o', 'StrictHostKeyChecking=accept-new',
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
                return jsonify({'success': True, 'status': 'online', 'message': 'SSH connection successful'})
            else:
                return jsonify({'success': False, 'status': 'offline', 'error': result.stderr or 'SSH connection failed'})

        except subprocess.TimeoutExpired:
            return jsonify({'success': False, 'status': 'offline', 'error': 'SSH connection timed out'})
        except Exception as e:
            return jsonify({'success': False, 'status': 'error', 'error': str(e)})

    elif connection_type == 'agent':
        # Test Agent connection
        agent_port = int(server.get('agent_port', 8090))
        agent_api_key = server.get('agent_api_key', '')

        try:
            # Call the agent's /health endpoint (no auth required)
            health_url = f'http://{host}:{agent_port}/health'
            health_req = Request(health_url, method='GET')
            health_req.add_header('Content-Type', 'application/json')

            try:
                with urlopen(health_req, timeout=10) as response:
                    if response.status == 200:
                        health_data = json.loads(response.read().decode('utf-8'))
                        agent_name = health_data.get('agent', 'Unknown')
                        version = health_data.get('version', 'Unknown')
                        return jsonify({
                            'success': True,
                            'status': 'online',
                            'message': f'Agent online',
                            'agent_name': agent_name,
                            'version': version
                        })
                    else:
                        return jsonify({'success': False, 'status': 'offline', 'error': 'Agent health check failed'})
            except (URLError, HTTPError) as e:
                return jsonify({'success': False, 'status': 'offline', 'error': f'Cannot reach agent: {str(e)}'})

        except Exception as e:
            return jsonify({'success': False, 'status': 'error', 'error': str(e)})

    else:
        return jsonify({'success': False, 'status': 'error', 'error': 'Invalid connection_type'})


# Database Configuration API Routes
@app.route('/api/databases', methods=['GET'])
@login_required
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

    required_fields = ['name', 'host', 'username', 'password']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    new_config = {
        'id': generate_id(),
        'name': data['name'],
        'type': data.get('type', 'mysql'),
        'host': data['host'],
        'port': int(data.get('port', 3306)),
        'username': data['username'],
        'password': data['password'],
        'databases': data.get('databases', '*'),
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
    """Test MySQL database connection via SSH or Agent"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Database config
    db_host = data.get('host', '')
    db_port = int(data.get('port', 3306))
    db_user = data.get('username', '')
    db_pass = data.get('password', '')

    # Server config
    server_id = data.get('server_id', '')

    if not all([db_host, db_user, db_pass]):
        return jsonify({'error': 'Missing required database fields'}), 400

    if not server_id:
        return jsonify({'error': 'Server selection required to test connection'}), 400

    # Get server details
    server = get_server(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 400

    connection_type = server.get('connection_type', 'ssh')

    # Use agent for testing if server is agent-based
    if connection_type == 'agent':
        try:
            agent_url = f"http://{server['host']}:{server.get('agent_port', 8090)}/test/database"

            payload = {
                'db_host': db_host,
                'db_port': db_port,
                'db_user': db_user,
                'db_password': db_pass
            }

            req_data = json.dumps(payload).encode('utf-8')
            req = Request(agent_url, data=req_data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-API-Key', server.get('agent_api_key', ''))

            with urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))

            if result.get('success'):
                return jsonify({'success': True, 'message': 'Database connection successful'})
            else:
                return jsonify({'error': result.get('error', 'Connection failed')}), 400

        except (URLError, HTTPError) as e:
            if hasattr(e, 'code') and e.code == 401:
                return jsonify({'error': 'Agent authentication failed - check API key'}), 400
            return jsonify({'error': sanitize_error_message(str(e))}), 400
        except Exception as e:
            return jsonify({'error': sanitize_error_message(str(e))}), 500

    # SSH-based testing
    try:
        # Escape all values for shell to prevent command injection
        escaped_host = shlex.quote(db_host)
        escaped_user = shlex.quote(db_user)
        escaped_pass = shlex.quote(db_pass)

        # Build SSH command to test MySQL connection
        ssh_key = server.get('ssh_key') or '/home/backupx/.ssh/id_rsa'
        ssh_cmd = [
            'ssh', '-i', ssh_key,
            '-p', str(server.get('ssh_port') or 22),
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=10',
            f"{server['ssh_user']}@{server['host']}",
            f"mysql -h {escaped_host} -P {db_port} -u {escaped_user} -p{escaped_pass} -e 'SELECT 1' 2>&1"
        ]

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

    # Initialize database
    init_db()
    migrate_json_to_sqlite()

    # Initialize PostgreSQL database and run migrations
    try:
        from .db import init_database
        init_database()
        logger.info("PostgreSQL database initialized and migrations applied")
    except Exception as e:
        logger.warning(f"PostgreSQL initialization skipped: {e}")

    # Re-initialize scheduler with timezone from database
    reinit_scheduler_with_db_timezone()

    # Initialize audit logging
    try:
        from .audit.logger import init_audit_logger
        from .db import get_database
        # Use a simple wrapper to make db compatible with audit logger
        class DBWrapper:
            def execute(self, query, params=None):
                conn = get_db_connection()
                if params:
                    conn.execute(query, params)
                else:
                    conn.execute(query)
                conn.commit()
                conn.close()
            def commit(self):
                pass  # Handled in execute
            def fetchall(self, query, params=None):
                conn = get_db_connection()
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
            def fetchone(self, query, params=None):
                conn = get_db_connection()
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        init_audit_logger(DBWrapper())
        logger.info("Audit logging initialized")
    except Exception as e:
        logger.warning(f"Audit logging not available: {e}")

    # Initialize schedules
    init_schedules()

    logger.info("BackupX application initialized successfully")


# Run initialization on module load (for gunicorn/uwsgi)
init_app()


if __name__ == '__main__':
    # Development server only - use gunicorn in production
    debug_mode = os.environ.get('DEBUG', '').lower() in ('true', '1', 'yes')
    if debug_mode:
        logger.info("Starting in DEBUG mode - DO NOT USE IN PRODUCTION")
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)

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
from datetime import datetime
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

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

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

# Scheduler
scheduler = BackgroundScheduler()
scheduler.start()


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
            ssh_port INTEGER DEFAULT 22,
            ssh_user TEXT NOT NULL,
            ssh_key TEXT DEFAULT '/home/backupx/.ssh/id_rsa',
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

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_history_job_id ON history(job_id);
    ''')
    conn.commit()
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
                    INSERT INTO servers (id, name, host, ssh_port, ssh_user, ssh_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (s['id'], s['name'], s['host'], s.get('ssh_port', 22), s['ssh_user'],
                      s.get('ssh_key', '/home/backupx/.ssh/id_rsa'), s.get('created_at', datetime.now().isoformat()),
                      s.get('updated_at')))
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
                    INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (c['id'], c['name'], c['endpoint'], c['bucket'], c['access_key'], c['secret_key'],
                      c.get('region', ''), c.get('created_at', datetime.now().isoformat()), c.get('updated_at')))
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
                      c.get('created_at', datetime.now().isoformat()), c.get('updated_at')))
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
                      j.get('status', 'pending'), j.get('created_at', datetime.now().isoformat()), j.get('updated_at'),
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

def load_jobs():
    """Load all backup jobs from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM jobs').fetchall()
    conn.close()

    jobs = {}
    for row in rows:
        job = dict(row)
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
        job = dict(row)
        job['directories'] = json.loads(job['directories'] or '[]')
        job['excludes'] = json.loads(job['excludes'] or '[]')
        job['schedule_enabled'] = bool(job['schedule_enabled'])
        return job
    return None


def save_job(job_id, job):
    """Save a job to database (insert or update)"""
    conn = get_db_connection()

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
              job.get('s3_endpoint'), job.get('s3_bucket'), job.get('s3_access_key'), job.get('s3_secret_key'),
              json.dumps(job.get('directories', [])), json.dumps(job.get('excludes', [])), job.get('database_config_id'),
              job.get('restic_password'), job.get('backup_prefix'), 1 if job.get('schedule_enabled') else 0,
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
              job.get('s3_endpoint'), job.get('s3_bucket'), job.get('s3_access_key'), job.get('s3_secret_key'),
              json.dumps(job.get('directories', [])), json.dumps(job.get('excludes', [])), job.get('database_config_id'),
              job.get('restic_password'), job.get('backup_prefix'), 1 if job.get('schedule_enabled') else 0,
              job.get('schedule_cron', '0 2 * * *'), job.get('retention_hourly', 24), job.get('retention_daily', 7),
              job.get('retention_weekly', 4), job.get('retention_monthly', 12), job.get('timeout', 7200),
              job.get('status', 'pending'), job.get('created_at', datetime.now().isoformat()), job.get('updated_at'),
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
                     (status, last_run, last_success, datetime.now().isoformat(), job_id))
    elif last_run:
        conn.execute('UPDATE jobs SET status=?, last_run=?, updated_at=? WHERE id=?',
                     (status, last_run, datetime.now().isoformat(), job_id))
    else:
        conn.execute('UPDATE jobs SET status=?, updated_at=? WHERE id=?',
                     (status, datetime.now().isoformat(), job_id))
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
    ''', (datetime.now().isoformat(), job_id, job_name, status, message, duration))

    # Keep only last 100 entries
    conn.execute('''
        DELETE FROM history WHERE id NOT IN (
            SELECT id FROM history ORDER BY timestamp DESC LIMIT 100
        )
    ''')
    conn.commit()
    conn.close()


# --- S3 Configs ---

def load_s3_configs():
    """Load S3 configurations from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM s3_configs').fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_s3_config(config_id):
    """Get a single S3 config by ID"""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM s3_configs WHERE id = ?', (config_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_s3_config(config):
    """Create a new S3 config"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (config['id'], config['name'], config['endpoint'], config['bucket'], config['access_key'],
          config['secret_key'], config.get('region', ''), config.get('created_at', datetime.now().isoformat()),
          config.get('updated_at')))
    conn.commit()
    conn.close()


def update_s3_config(config_id, config):
    """Update an S3 config"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE s3_configs SET name=?, endpoint=?, bucket=?, access_key=?, secret_key=?, region=?, updated_at=?
        WHERE id=?
    ''', (config['name'], config['endpoint'], config['bucket'], config['access_key'],
          config['secret_key'], config.get('region', ''), datetime.now().isoformat(), config_id))
    conn.commit()
    conn.close()


def delete_s3_config(config_id):
    """Delete an S3 config"""
    conn = get_db_connection()
    conn.execute('DELETE FROM s3_configs WHERE id = ?', (config_id,))
    conn.commit()
    conn.close()


# --- Servers ---

def load_servers():
    """Load servers from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM servers').fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_server(server_id):
    """Get a single server by ID"""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM servers WHERE id = ?', (server_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_server(server):
    """Create a new server"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO servers (id, name, host, ssh_port, ssh_user, ssh_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (server['id'], server['name'], server['host'], server.get('ssh_port', 22), server['ssh_user'],
          server.get('ssh_key', '/home/backupx/.ssh/id_rsa'), server.get('created_at', datetime.now().isoformat()),
          server.get('updated_at')))
    conn.commit()
    conn.close()


def update_server_in_db(server_id, server):
    """Update a server"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE servers SET name=?, host=?, ssh_port=?, ssh_user=?, ssh_key=?, updated_at=?
        WHERE id=?
    ''', (server['name'], server['host'], server.get('ssh_port', 22), server['ssh_user'],
          server.get('ssh_key', '/home/backupx/.ssh/id_rsa'), datetime.now().isoformat(), server_id))
    conn.commit()
    conn.close()


def delete_server_from_db(server_id):
    """Delete a server"""
    conn = get_db_connection()
    conn.execute('DELETE FROM servers WHERE id = ?', (server_id,))
    conn.commit()
    conn.close()


# --- Database Configs ---

def load_db_configs():
    """Load database configurations from database"""
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM db_configs').fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_db_config(config_id):
    """Get a single database config by ID"""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM db_configs WHERE id = ?', (config_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_db_config(config):
    """Create a new database config"""
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO db_configs (id, name, type, host, port, username, password, databases, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (config['id'], config['name'], config.get('type', 'mysql'), config['host'], config.get('port', 3306),
          config['username'], config['password'], config.get('databases', '*'),
          config.get('created_at', datetime.now().isoformat()), config.get('updated_at')))
    conn.commit()
    conn.close()


def update_db_config_in_db(config_id, config):
    """Update a database config"""
    conn = get_db_connection()
    conn.execute('''
        UPDATE db_configs SET name=?, type=?, host=?, port=?, username=?, password=?, databases=?, updated_at=?
        WHERE id=?
    ''', (config['name'], config.get('type', 'mysql'), config['host'], config.get('port', 3306),
          config['username'], config['password'], config.get('databases', '*'),
          datetime.now().isoformat(), config_id))
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
          1 if channel.get('notify_on_failure', True) else 0, channel.get('created_at', datetime.now().isoformat()),
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
          1 if channel.get('notify_on_failure', True) else 0, datetime.now().isoformat(), channel_id))
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
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

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
            "timestamp": datetime.now().isoformat()
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
        "timestamp": datetime.now().isoformat()
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
            elif channel['type'] == 'webhook':
                send_webhook_notification(channel, job_name, status, message, duration)
        except Exception as e:
            logger.error(f"Failed to send {channel['type']} notification to {channel['name']}: {e}")


def run_backup(job_id):
    """Execute a backup job"""
    job = get_job(job_id)
    if not job:
        return False, "Job not found"

    backup_type = job.get('backup_type', 'filesystem')

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
    start_time = datetime.now()
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
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=30',
            job['remote_host']
        ]

        # Build remote command with proper escaping
        remote_cmd = f"""
export AWS_ACCESS_KEY_ID={s3_access_key}
export AWS_SECRET_ACCESS_KEY={s3_secret_key}
export RESTIC_PASSWORD={restic_password}
export RESTIC_REPOSITORY="s3:https://{job['s3_endpoint']}/{job['s3_bucket']}/{job['backup_prefix']}"
restic backup --compression auto --tag automated {' '.join(exclude_args)} {directories}
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
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=datetime.now().isoformat())
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
    start_time = datetime.now()
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
            '-o', 'StrictHostKeyChecking=no',
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
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
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
restic backup --compression auto --tag automated --tag mysql-backup "$BACKUP_FILE"
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

        duration = (datetime.now() - start_time).total_seconds()

        if result.returncode == 0:
            update_job_status(job_id, 'success', last_run=start_time.isoformat(), last_success=datetime.now().isoformat())
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
        'ssh_port': server['ssh_port'] if server else int(data.get('ssh_port', 22)),
        'ssh_key': server['ssh_key'] if server else data.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
        's3_endpoint': s3_config['endpoint'] if s3_config else data.get('s3_endpoint'),
        's3_bucket': s3_config['bucket'] if s3_config else data.get('s3_bucket'),
        's3_access_key': s3_config['access_key'] if s3_config else data.get('s3_access_key'),
        's3_secret_key': s3_config['secret_key'] if s3_config else data.get('s3_secret_key'),
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
        'created_at': datetime.now().isoformat()
    }

    save_job(job_id, job)

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
        'ssh_port': server['ssh_port'] if server else job.get('ssh_port', 22),
        'ssh_key': server['ssh_key'] if server else job.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
        's3_endpoint': s3_config['endpoint'] if s3_config else job.get('s3_endpoint'),
        's3_bucket': s3_config['bucket'] if s3_config else job.get('s3_bucket'),
        's3_access_key': s3_config['access_key'] if s3_config else job.get('s3_access_key'),
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
        'updated_at': datetime.now().isoformat()
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

    return jsonify({'success': True})


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
@login_required
@csrf.exempt
def api_delete_job(job_id):
    """Delete a backup job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    delete_job_from_db(job_id)

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
    job = get_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'}), 404

    # Run in background
    thread = threading.Thread(target=run_backup, args=[job_id])
    thread.start()

    return jsonify({'success': True, 'message': 'Backup started'})


@app.route('/api/jobs/<job_id>/snapshots')
@login_required
def api_job_snapshots(job_id):
    """Get snapshots for a job"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

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
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
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
def api_create_server_route():
    """Create a new server"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required_fields = ['name', 'host', 'ssh_user']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    # Validate inputs
    if not validate_hostname(data['host']):
        return jsonify({'error': 'Invalid hostname or IP address'}), 400

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
        'ssh_port': ssh_port,
        'ssh_user': data['ssh_user'],
        'ssh_key': ssh_key,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }

    create_server(new_server)

    return jsonify(new_server), 201


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

    # Update fields
    server['name'] = data.get('name', server['name'])
    server['host'] = data.get('host', server['host'])
    server['ssh_port'] = int(data.get('ssh_port', server.get('ssh_port', 22)))
    server['ssh_user'] = data.get('ssh_user', server['ssh_user'])
    server['ssh_key'] = data.get('ssh_key', server.get('ssh_key', '/home/backupx/.ssh/id_rsa'))

    update_server_in_db(server_id, server)

    return jsonify(server)


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
    ssh_port = int(data.get('ssh_port', 22))
    ssh_user = data.get('ssh_user', '')
    ssh_key = data.get('ssh_key', '/home/backupx/.ssh/id_rsa')

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
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
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
    """Test MySQL database connection via SSH"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Database config
    db_host = data.get('host', '')
    db_port = int(data.get('port', 3306))
    db_user = data.get('username', '')
    db_pass = data.get('password', '')

    # Server config (SSH connection)
    server_id = data.get('server_id', '')

    if not all([db_host, db_user, db_pass]):
        return jsonify({'error': 'Missing required database fields'}), 400

    if not server_id:
        return jsonify({'error': 'Server selection required to test connection'}), 400

    # Get server details
    servers = load_servers()
    server = next((s for s in servers if s['id'] == server_id), None)
    if not server:
        return jsonify({'error': 'Server not found'}), 400

    try:
        # Escape all values for shell to prevent command injection
        escaped_host = shlex.quote(db_host)
        escaped_user = shlex.quote(db_user)
        escaped_pass = shlex.quote(db_pass)

        # Build SSH command to test MySQL connection
        ssh_cmd = [
            'ssh', '-i', server.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
            '-p', str(server.get('ssh_port', 22)),
            '-o', 'StrictHostKeyChecking=no',
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
    valid_types = ['email', 'slack', 'discord', 'webhook']
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
        'created_at': datetime.now().isoformat()
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
        elif channel_type == 'webhook':
            send_webhook_notification(channel, 'Test Job', 'success', 'This is a test notification from BackupX', 0)
        else:
            return jsonify({'error': f'Unknown channel type: {channel_type}'}), 400

        return jsonify({'success': True, 'message': 'Test notification sent successfully'})
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


# Initialize database and migrate data on startup
def init_app():
    """Initialize the application"""
    logger.info("Initializing BackupX application...")
    init_db()
    migrate_json_to_sqlite()
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

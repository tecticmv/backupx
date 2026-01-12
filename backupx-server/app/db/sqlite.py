"""
SQLite database backend implementation.
"""

import sqlite3
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import DatabaseBackend

logger = logging.getLogger(__name__)


class SQLiteBackend(DatabaseBackend):
    """SQLite database backend with thread-local connections."""

    def __init__(self, database_path: str):
        """
        Initialize SQLite backend.

        Args:
            database_path: Path to SQLite database file
        """
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        logger.info(f"SQLite backend initialized: {self.database_path}")

    def _get_thread_connection(self) -> sqlite3.Connection:
        """Get or create connection for current thread."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self.database_path),
                check_same_thread=False
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection for the current thread."""
        return self._get_thread_connection()

    def close(self) -> None:
        """Close the connection for the current thread."""
        if hasattr(self._local, 'connection') and self._local.connection is not None:
            self._local.connection.close()
            self._local.connection = None

    def execute(self, query: str, params: Optional[Tuple] = None) -> sqlite3.Cursor:
        """Execute a query without returning results."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor

    def executemany(self, query: str, params_list: List[Tuple]) -> sqlite3.Cursor:
        """Execute a query multiple times with different parameters."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.executemany(query, params_list)
        return cursor

    def executescript(self, script: str) -> None:
        """Execute multiple SQL statements."""
        conn = self.get_connection()
        conn.executescript(script)

    def fetchone(self, query: str, params: Optional[Tuple] = None) -> Optional[Dict[str, Any]]:
        """Execute a query and fetch one result as a dictionary."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        row = cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """Execute a query and fetch all results as dictionaries."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def commit(self) -> None:
        """Commit the current transaction."""
        conn = self.get_connection()
        conn.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        conn = self.get_connection()
        conn.rollback()

    def get_table_columns(self, table_name: str) -> List[str]:
        """Get list of column names for a table."""
        rows = self.fetchall(f"PRAGMA table_info({table_name})")
        return [row['name'] for row in rows]

    def add_column(self, table_name: str, column_name: str, column_type: str, default: Optional[str] = None) -> None:
        """Add a column to an existing table."""
        default_clause = f" DEFAULT {default}" if default is not None else ""
        query = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}{default_clause}"
        self.execute(query)
        self.commit()
        logger.info(f"Added column {column_name} to table {table_name}")

    @property
    def placeholder(self) -> str:
        """SQLite uses '?' as placeholder."""
        return '?'

    def init_schema(self) -> None:
        """Initialize SQLite database schema."""
        schema = '''
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

            -- Audit log table
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

            -- Scheduler tables for distributed mode
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
        '''
        self.executescript(schema)
        self.commit()
        logger.info("SQLite schema initialized")

    def migrate_schema(self) -> None:
        """Run schema migrations for existing databases."""
        # Check and add columns to servers table
        columns = self.get_table_columns('servers')
        if 'connection_type' not in columns:
            self.add_column('servers', 'connection_type', 'TEXT', "'ssh'")
        if 'agent_port' not in columns:
            self.add_column('servers', 'agent_port', 'INTEGER', '8090')
        if 'agent_api_key' not in columns:
            self.add_column('servers', 'agent_api_key', 'TEXT')

        logger.info("SQLite schema migration completed")

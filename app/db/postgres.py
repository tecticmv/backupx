"""
PostgreSQL database backend implementation with connection pooling.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .base import DatabaseBackend

logger = logging.getLogger(__name__)

# Import psycopg2 conditionally
try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    logger.warning("psycopg2 not installed. PostgreSQL support unavailable.")


class PostgresBackend(DatabaseBackend):
    """PostgreSQL database backend with connection pooling."""

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 5432,
        database: str = 'backupx',
        user: str = 'backupx',
        password: str = '',
        pool_min: int = 2,
        pool_max: int = 10,
        ssl_mode: str = 'prefer'
    ):
        """
        Initialize PostgreSQL backend with connection pool.

        Args:
            host: Database host
            port: Database port
            database: Database name
            user: Database user
            password: Database password
            pool_min: Minimum pool connections
            pool_max: Maximum pool connections
            ssl_mode: SSL mode (disable, prefer, require)
        """
        if not PSYCOPG2_AVAILABLE:
            raise ImportError("psycopg2 is required for PostgreSQL support. Install with: pip install psycopg2-binary")

        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.ssl_mode = ssl_mode

        # Create connection pool
        self._pool = pool.ThreadedConnectionPool(
            minconn=pool_min,
            maxconn=pool_max,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            sslmode=ssl_mode,
            cursor_factory=RealDictCursor
        )

        # Thread-local storage for connections
        import threading
        self._local = threading.local()

        logger.info(f"PostgreSQL backend initialized: {user}@{host}:{port}/{database} (pool: {pool_min}-{pool_max})")

    def _get_thread_connection(self):
        """Get or create connection for current thread from pool."""
        if not hasattr(self._local, 'connection') or self._local.connection is None or self._local.connection.closed:
            self._local.connection = self._pool.getconn()
        return self._local.connection

    def _release_thread_connection(self):
        """Release connection back to pool for current thread."""
        if hasattr(self._local, 'connection') and self._local.connection is not None:
            self._pool.putconn(self._local.connection)
            self._local.connection = None

    def get_connection(self):
        """Get a database connection from the pool."""
        return self._get_thread_connection()

    def close(self) -> None:
        """Release the connection back to the pool."""
        self._release_thread_connection()

    def close_all(self) -> None:
        """Close all connections in the pool."""
        self._pool.closeall()
        logger.info("PostgreSQL connection pool closed")

    def convert_query(self, query: str) -> str:
        """Convert '?' placeholders to '%s' for PostgreSQL."""
        # Simple replacement - handles most cases
        # Note: This doesn't handle '?' inside string literals perfectly
        return query.replace('?', '%s')

    def execute(self, query: str, params: Optional[Tuple] = None):
        """Execute a query without returning results."""
        conn = self.get_connection()
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        if params:
            cursor.execute(converted_query, params)
        else:
            cursor.execute(converted_query)
        return cursor

    def executemany(self, query: str, params_list: List[Tuple]):
        """Execute a query multiple times with different parameters."""
        conn = self.get_connection()
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        cursor.executemany(converted_query, params_list)
        return cursor

    def executescript(self, script: str) -> None:
        """Execute multiple SQL statements."""
        conn = self.get_connection()
        cursor = conn.cursor()
        # PostgreSQL can execute multiple statements in one execute call
        cursor.execute(script)

    def fetchone(self, query: str, params: Optional[Tuple] = None) -> Optional[Dict[str, Any]]:
        """Execute a query and fetch one result as a dictionary."""
        conn = self.get_connection()
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        if params:
            cursor.execute(converted_query, params)
        else:
            cursor.execute(converted_query)
        row = cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """Execute a query and fetch all results as dictionaries."""
        conn = self.get_connection()
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        if params:
            cursor.execute(converted_query, params)
        else:
            cursor.execute(converted_query)
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
        """Get list of column names for a table using information_schema."""
        query = """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """
        rows = self.fetchall(query, (table_name,))
        return [row['column_name'] for row in rows]

    def add_column(self, table_name: str, column_name: str, column_type: str, default: Optional[str] = None) -> None:
        """Add a column to an existing table."""
        # Convert SQLite types to PostgreSQL
        pg_type = self._convert_type(column_type)
        default_clause = f" DEFAULT {default}" if default is not None else ""
        query = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {pg_type}{default_clause}"
        self.execute(query)
        self.commit()
        logger.info(f"Added column {column_name} to table {table_name}")

    def _convert_type(self, sqlite_type: str) -> str:
        """Convert SQLite type to PostgreSQL type."""
        type_map = {
            'INTEGER': 'INTEGER',
            'TEXT': 'TEXT',
            'REAL': 'DOUBLE PRECISION',
            'BLOB': 'BYTEA',
        }
        upper_type = sqlite_type.upper()
        return type_map.get(upper_type, upper_type)

    @property
    def placeholder(self) -> str:
        """PostgreSQL uses '%s' as placeholder."""
        return '%s'

    def init_schema(self) -> None:
        """Initialize PostgreSQL database schema."""
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
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
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
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
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
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
            );

            -- Jobs table
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                backup_type TEXT DEFAULT 'filesystem',
                server_id TEXT REFERENCES servers(id),
                s3_config_id TEXT REFERENCES s3_configs(id),
                remote_host TEXT,
                ssh_port INTEGER DEFAULT 22,
                ssh_key TEXT,
                s3_endpoint TEXT,
                s3_bucket TEXT,
                s3_access_key TEXT,
                s3_secret_key TEXT,
                directories TEXT,
                excludes TEXT,
                database_config_id TEXT REFERENCES db_configs(id),
                restic_password TEXT,
                backup_prefix TEXT,
                schedule_enabled BOOLEAN DEFAULT FALSE,
                schedule_cron TEXT DEFAULT '0 2 * * *',
                retention_hourly INTEGER DEFAULT 24,
                retention_daily INTEGER DEFAULT 7,
                retention_weekly INTEGER DEFAULT 4,
                retention_monthly INTEGER DEFAULT 12,
                timeout INTEGER DEFAULT 7200,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP,
                last_run TIMESTAMP,
                last_success TIMESTAMP
            );

            -- History table
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                job_id TEXT NOT NULL,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                duration DOUBLE PRECISION DEFAULT 0
            );

            -- Notification channels table
            CREATE TABLE IF NOT EXISTS notification_channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                config TEXT NOT NULL,
                notify_on_success BOOLEAN DEFAULT TRUE,
                notify_on_failure BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP
            );

            -- Audit log table
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                acquired_at TIMESTAMP,
                heartbeat_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                cron_expression TEXT NOT NULL,
                next_run TIMESTAMP,
                last_run TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );

            -- Application settings table (key-value store)
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        logger.info("PostgreSQL schema initialized")

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

        # Add SSH auth columns to servers table
        if 'ssh_auth_type' not in columns:
            self.add_column('servers', 'ssh_auth_type', 'TEXT', "'key_path'")
        if 'ssh_password' not in columns:
            self.add_column('servers', 'ssh_password', 'TEXT')
        if 'ssh_key_content' not in columns:
            self.add_column('servers', 'ssh_key_content', 'TEXT')

        # Add docker_container column to db_configs for docker-exec backups
        db_columns = self.get_table_columns('db_configs')
        if 'docker_container' not in db_columns:
            self.add_column('db_configs', 'docker_container', 'TEXT')

        # Create app_settings table if it doesn't exist
        self.executescript('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        self.commit()

        logger.info("PostgreSQL schema migration completed")

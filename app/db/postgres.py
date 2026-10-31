"""
PostgreSQL database backend implementation with connection pooling.
"""

import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from .base import DatabaseBackend

logger = logging.getLogger(__name__)

# Two-key advisory lock namespace for schema init/migrations (next to the
# scheduler/job namespaces in app/scheduler/locks.py)
SCHEMA_LOCK_NAMESPACE = 0x42585303

# Import psycopg2 conditionally
try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    logger.warning("psycopg2 not installed. PostgreSQL support unavailable.")


if PSYCOPG2_AVAILABLE:
    class BlockingThreadedConnectionPool(pool.ThreadedConnectionPool):
        """ThreadedConnectionPool that waits for a free connection instead of failing.

        psycopg2's pool raises PoolError the moment all maxconn connections are
        checked out. Here a caller waits up to `timeout` seconds for one to be
        returned, and only then gets PoolError.
        """

        def __init__(self, minconn, maxconn, *args, timeout: float = 30, **kwargs):
            self._slots = threading.BoundedSemaphore(maxconn)
            self._timeout = timeout
            super().__init__(minconn, maxconn, *args, **kwargs)

        def getconn(self, key=None):
            if not self._slots.acquire(timeout=self._timeout):
                raise pool.PoolError(
                    f"connection pool exhausted: no connection free after {self._timeout:g}s")
            try:
                return super().getconn(key)
            except Exception:
                self._slots.release()
                raise

        def putconn(self, conn=None, key=None, close=False):
            super().putconn(conn, key, close)
            self._slots.release()


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
        pool_max: int = 30,
        ssl_mode: str = 'prefer',
        pool_timeout: float = 30
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
            pool_timeout: Seconds to wait for a free pooled connection before PoolError
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
        self._pool = BlockingThreadedConnectionPool(
            minconn=pool_min,
            maxconn=pool_max,
            timeout=pool_timeout,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            sslmode=ssl_mode,
            cursor_factory=RealDictCursor
        )

        # Thread-local storage for connections
        self._local = threading.local()

        logger.info(f"PostgreSQL backend initialized: {user}@{host}:{port}/{database} (pool: {pool_min}-{pool_max})")

    def _get_thread_connection(self):
        """Get or create connection for current thread from pool."""
        conn = getattr(self._local, 'connection', None)
        if conn is not None and conn.closed:
            # Dropped (e.g. Postgres restarted): give its slot back before taking another
            self._local.connection = None
            try:
                self._pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = None
        if conn is None:
            self._local.connection = self._pool.getconn()
        return self._local.connection

    def _release_thread_connection(self):
        """Release connection back to pool for current thread."""
        conn = getattr(self._local, 'connection', None)
        if conn is not None:
            self._local.connection = None
            self._pool.putconn(conn)

    def get_connection(self):
        """Get a database connection from the pool."""
        return self._get_thread_connection()

    @contextmanager
    def short_lived_connections(self):
        """Within this block, give the thread's connection back to the pool after each operation.

        Normally a thread keeps its pooled connection until close() (end of the
        request). Long-running work such as a backup run would then hold a
        connection for hours, so concurrent runs exhaust the pool. In this mode
        the connection goes back as soon as nothing is pending on it: after
        commit/rollback, and after a read that started its own transaction.
        """
        previous = getattr(self._local, 'short_lived', False)
        self._local.short_lived = True
        try:
            yield
        finally:
            self._local.short_lived = previous
            if not previous:
                self._release_thread_connection()

    def _idle_before(self, conn) -> bool:
        return conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_IDLE

    def _maybe_release(self, read_only_txn: bool = False):
        """Release the thread's connection in short-lived mode if nothing is pending on it.

        read_only_txn: the transaction was opened by the read just done, so
        rolling it back loses nothing.
        """
        if not getattr(self._local, 'short_lived', False):
            return
        conn = getattr(self._local, 'connection', None)
        if conn is None:
            return
        if conn.closed:
            self._local.connection = None
            self._pool.putconn(conn, close=True)
            return
        if self._idle_before(conn) or read_only_txn:
            if not self._idle_before(conn):
                conn.rollback()
            self._release_thread_connection()

    def open_dedicated_connection(self):
        """Open an autocommit connection outside the pool, for session-scoped advisory locks.

        The caller owns it and must close it. TCP keepalives on both ends make
        a dead peer show up within about a minute, so a lock held by a process
        that vanished is released and one held over a broken link is noticed.
        """
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
            sslmode=self.ssl_mode,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            options='-c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10 -c tcp_keepalives_count=3',
            application_name='backupx-lock',
        )
        conn.autocommit = True
        return conn

    @contextmanager
    def schema_lock(self):
        """Serialize schema creation and migrations across workers and containers.

        Every gunicorn worker runs init_schema/migrate_schema at import. Run
        concurrently, two CREATE TABLE IF NOT EXISTS for the same table can both
        miss it and one fails on the table's row type ("already exists"),
        killing the worker and the gunicorn master. Holding a session advisory
        lock on a dedicated connection makes one worker migrate while the
        others wait, then find nothing to do. Closing the connection releases it
        (also if this process dies mid-migration).
        """
        conn = self.open_dedicated_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_lock(%s, %s)', (SCHEMA_LOCK_NAMESPACE, 0))
            yield
        finally:
            conn.close()

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
        try:
            if params:
                cursor.execute(converted_query, params)
            else:
                cursor.execute(converted_query)
            return cursor
        except Exception:
            conn.rollback()
            self._maybe_release()
            raise

    def executemany(self, query: str, params_list: List[Tuple]):
        """Execute a query multiple times with different parameters."""
        conn = self.get_connection()
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        try:
            cursor.executemany(converted_query, params_list)
            return cursor
        except Exception:
            conn.rollback()
            raise

    def executescript(self, script: str) -> None:
        """Execute multiple SQL statements."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # PostgreSQL can execute multiple statements in one execute call
            cursor.execute(script)
        except Exception:
            conn.rollback()
            raise

    def fetchone(self, query: str, params: Optional[Tuple] = None) -> Optional[Dict[str, Any]]:
        """Execute a query and fetch one result as a dictionary."""
        conn = self.get_connection()
        fresh_txn = self._idle_before(conn)
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        try:
            if params:
                cursor.execute(converted_query, params)
            else:
                cursor.execute(converted_query)
            row = cursor.fetchone()
            result = dict(row) if row else None
        except Exception:
            conn.rollback()
            self._maybe_release()
            raise
        self._maybe_release(read_only_txn=fresh_txn)
        return result

    def fetchall(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """Execute a query and fetch all results as dictionaries."""
        conn = self.get_connection()
        fresh_txn = self._idle_before(conn)
        cursor = conn.cursor()
        converted_query = self.convert_query(query)
        try:
            if params:
                cursor.execute(converted_query, params)
            else:
                cursor.execute(converted_query)
            rows = cursor.fetchall()
            result = [dict(row) for row in rows]
        except Exception:
            conn.rollback()
            self._maybe_release()
            raise
        self._maybe_release(read_only_txn=fresh_txn)
        return result

    def commit(self) -> None:
        """Commit the current transaction."""
        conn = self.get_connection()
        conn.commit()
        self._maybe_release()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        conn = self.get_connection()
        conn.rollback()
        self._maybe_release()

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
                ssh_auth_type TEXT DEFAULT 'key_path',
                ssh_password TEXT,
                ssh_key_content TEXT,
                agent_port INTEGER DEFAULT 8090,
                agent_api_key TEXT,
                status TEXT DEFAULT 'active',
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
                skip_ssl_verify BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT 'active',
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
                docker_container TEXT,
                status TEXT DEFAULT 'active',
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
                s3_config_ids TEXT,
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
                skip_ssl_verify BOOLEAN DEFAULT FALSE,
                progress INTEGER DEFAULT 0,
                progress_message TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP,
                last_run TIMESTAMP,
                last_success TIMESTAMP,
                pitr_enabled BOOLEAN DEFAULT FALSE,
                pitr_interval INTEGER DEFAULT 5,
                pitr_retention_days INTEGER DEFAULT 7,
                pitr_client_image TEXT
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

            -- Retention runs (restic forget after backups, weekly prune)
            CREATE TABLE IF NOT EXISTS retention_runs (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                job_id TEXT NOT NULL,
                storage_id TEXT NOT NULL DEFAULT '',
                storage_name TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                dry_run BOOLEAN DEFAULT FALSE,
                snapshots_removed INTEGER,
                bytes_freed BIGINT,
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_retention_runs_latest
                ON retention_runs(job_id, storage_id, action, timestamp DESC);

            -- Point-in-time recovery (app/pitr*.py)
            CREATE TABLE IF NOT EXISTS pitr_state (
                job_id TEXT PRIMARY KEY,
                kind TEXT,
                state TEXT,
                state_since TIMESTAMP,
                alerted_state TEXT,
                chain_id INTEGER DEFAULT 0,
                server_uuid TEXT,
                binlog_base TEXT,
                last_seq BIGINT,
                partial_file TEXT,
                partial_size BIGINT,
                partial_mtime BIGINT,
                partial_until TIMESTAMP,
                window_start TIMESTAMP,
                window_end TIMESTAMP,
                lag_minutes DOUBLE PRECISION,
                streamer_active TEXT,
                heartbeat TIMESTAMP,
                server_version TEXT,
                last_tick_at TIMESTAMP,
                last_ship_at TIMESTAMP,
                last_error TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                info TEXT,
                updated_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pitr_chains (
                job_id TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                start_seq BIGINT,
                server_uuid TEXT,
                base TEXT,
                reason TEXT,
                started_at TIMESTAMP,
                PRIMARY KEY (job_id, chain_id)
            );
            CREATE TABLE IF NOT EXISTS pitr_segments (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'closed',
                first_file TEXT,
                last_file TEXT,
                first_seq BIGINT,
                last_seq BIGINT,
                end_time TIMESTAMP,
                size BIGINT,
                snapshots TEXT,
                forgotten BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pitr_segments_job ON pitr_segments(job_id, kind, chain_id);
            CREATE TABLE IF NOT EXISTS pitr_anchors (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                time TIMESTAMP NOT NULL,
                binlog_file TEXT NOT NULL,
                binlog_pos BIGINT NOT NULL,
                gtid_set TEXT,
                server_uuid TEXT DEFAULT '',
                snapshots TEXT,
                forgotten BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pitr_anchors_job ON pitr_anchors(job_id, time);

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
        # Servers table migrations
        columns = self.get_table_columns('servers')
        if 'connection_type' not in columns:
            self.add_column('servers', 'connection_type', 'TEXT', "'ssh'")
        if 'agent_port' not in columns:
            self.add_column('servers', 'agent_port', 'INTEGER', '8090')
        if 'agent_api_key' not in columns:
            self.add_column('servers', 'agent_api_key', 'TEXT')
        if 'ssh_auth_type' not in columns:
            self.add_column('servers', 'ssh_auth_type', 'TEXT', "'key_path'")
        if 'ssh_password' not in columns:
            self.add_column('servers', 'ssh_password', 'TEXT')
        if 'ssh_key_content' not in columns:
            self.add_column('servers', 'ssh_key_content', 'TEXT')
        if 'status' not in columns:
            self.add_column('servers', 'status', 'TEXT', "'active'")

        # S3 configs migrations
        s3_columns = self.get_table_columns('s3_configs')
        if 'skip_ssl_verify' not in s3_columns:
            self.add_column('s3_configs', 'skip_ssl_verify', 'BOOLEAN', 'FALSE')
        if 'status' not in s3_columns:
            self.add_column('s3_configs', 'status', 'TEXT', "'active'")

        # DB configs migrations
        db_columns = self.get_table_columns('db_configs')
        if 'docker_container' not in db_columns:
            self.add_column('db_configs', 'docker_container', 'TEXT')
        if 'status' not in db_columns:
            self.add_column('db_configs', 'status', 'TEXT', "'active'")

        # Jobs migrations
        job_columns = self.get_table_columns('jobs')
        if 'skip_ssl_verify' not in job_columns:
            self.add_column('jobs', 'skip_ssl_verify', 'BOOLEAN', 'FALSE')
        if 'progress' not in job_columns:
            self.add_column('jobs', 'progress', 'INTEGER', '0')
        if 'progress_message' not in job_columns:
            self.add_column('jobs', 'progress_message', 'TEXT')
        if 's3_config_ids' not in job_columns:
            # JSON list of storage IDs, primary first; s3_config_id keeps the primary
            self.add_column('jobs', 's3_config_ids', 'TEXT')
        # Point-in-time recovery settings
        if 'pitr_enabled' not in job_columns:
            self.add_column('jobs', 'pitr_enabled', 'BOOLEAN', 'FALSE')
        if 'pitr_interval' not in job_columns:
            self.add_column('jobs', 'pitr_interval', 'INTEGER', '5')
        if 'pitr_retention_days' not in job_columns:
            self.add_column('jobs', 'pitr_retention_days', 'INTEGER', '7')
        if 'pitr_client_image' not in job_columns:
            self.add_column('jobs', 'pitr_client_image', 'TEXT')

        # Create app_settings table if it doesn't exist
        self.executescript('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Retention runs (restic forget after backups, weekly prune)
            CREATE TABLE IF NOT EXISTS retention_runs (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                job_id TEXT NOT NULL,
                storage_id TEXT NOT NULL DEFAULT '',
                storage_name TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                dry_run BOOLEAN DEFAULT FALSE,
                snapshots_removed INTEGER,
                bytes_freed BIGINT,
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_retention_runs_latest
                ON retention_runs(job_id, storage_id, action, timestamp DESC);

            -- Point-in-time recovery (app/pitr*.py)
            CREATE TABLE IF NOT EXISTS pitr_state (
                job_id TEXT PRIMARY KEY,
                kind TEXT,
                state TEXT,
                state_since TIMESTAMP,
                alerted_state TEXT,
                chain_id INTEGER DEFAULT 0,
                server_uuid TEXT,
                binlog_base TEXT,
                last_seq BIGINT,
                partial_file TEXT,
                partial_size BIGINT,
                partial_mtime BIGINT,
                partial_until TIMESTAMP,
                window_start TIMESTAMP,
                window_end TIMESTAMP,
                lag_minutes DOUBLE PRECISION,
                streamer_active TEXT,
                heartbeat TIMESTAMP,
                server_version TEXT,
                last_tick_at TIMESTAMP,
                last_ship_at TIMESTAMP,
                last_error TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                info TEXT,
                updated_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pitr_chains (
                job_id TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                start_seq BIGINT,
                server_uuid TEXT,
                base TEXT,
                reason TEXT,
                started_at TIMESTAMP,
                PRIMARY KEY (job_id, chain_id)
            );
            CREATE TABLE IF NOT EXISTS pitr_segments (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'closed',
                first_file TEXT,
                last_file TEXT,
                first_seq BIGINT,
                last_seq BIGINT,
                end_time TIMESTAMP,
                size BIGINT,
                snapshots TEXT,
                forgotten BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pitr_segments_job ON pitr_segments(job_id, kind, chain_id);
            CREATE TABLE IF NOT EXISTS pitr_anchors (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                time TIMESTAMP NOT NULL,
                binlog_file TEXT NOT NULL,
                binlog_pos BIGINT NOT NULL,
                gtid_set TEXT,
                server_uuid TEXT DEFAULT '',
                snapshots TEXT,
                forgotten BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pitr_anchors_job ON pitr_anchors(job_id, time);
        ''')
        self.commit()

        # Sync SERIAL sequences with existing data to prevent duplicate key errors.
        # The is_called flag is false when the table is empty, so the sequence stays
        # at 1 for the first row — setval(seq, 0) would be out of bounds.
        for table, seq in [('history', 'history_id_seq'), ('audit_log', 'audit_log_id_seq')]:
            self.execute(
                f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT MAX(id) FROM {table}) IS NOT NULL)"
            )
        self.commit()

        logger.info("PostgreSQL schema migration completed")

"""
Database migration utilities.
Supports migrating data from SQLite to PostgreSQL.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


def migrate_sqlite_to_postgres(sqlite_path: str, postgres_config: Dict[str, Any]) -> None:
    """
    Migrate all data from SQLite to PostgreSQL.

    Args:
        sqlite_path: Path to SQLite database file
        postgres_config: PostgreSQL connection configuration dict with keys:
            host, port, database, user, password, ssl_mode (optional)
    """
    from .sqlite import SQLiteBackend
    from .postgres import PostgresBackend

    logger.info(f"Starting migration from SQLite ({sqlite_path}) to PostgreSQL")

    # Connect to both databases
    sqlite_db = SQLiteBackend(sqlite_path)
    postgres_db = PostgresBackend(
        host=postgres_config.get('host', 'localhost'),
        port=postgres_config.get('port', 5432),
        database=postgres_config.get('database', 'backupx'),
        user=postgres_config['user'],
        password=postgres_config.get('password', ''),
        ssl_mode=postgres_config.get('ssl_mode', 'prefer')
    )

    try:
        # Initialize PostgreSQL schema
        postgres_db.init_schema()

        # Migration order matters due to foreign keys
        tables = [
            'servers',
            's3_configs',
            'db_configs',
            'jobs',
            'history',
            'notification_channels',
            'audit_log',
            'scheduler_lock',
            'scheduled_jobs'
        ]

        for table in tables:
            _migrate_table(sqlite_db, postgres_db, table)

        logger.info("Migration completed successfully")

    finally:
        sqlite_db.close()
        postgres_db.close_all()


def _migrate_table(sqlite_db, postgres_db, table_name: str) -> None:
    """Migrate a single table from SQLite to PostgreSQL."""
    logger.info(f"Migrating table: {table_name}")

    # Check if table exists in SQLite
    try:
        rows = sqlite_db.fetchall(f'SELECT * FROM {table_name}')
    except Exception as e:
        logger.warning(f"Table {table_name} does not exist in SQLite or error reading: {e}")
        return

    if not rows:
        logger.info(f"Table {table_name} is empty, skipping")
        return

    # Get column names from first row
    columns = list(rows[0].keys())

    # Build INSERT query with ON CONFLICT for upsert behavior
    placeholders = ', '.join(['%s'] * len(columns))
    column_list = ', '.join(columns)

    # Determine primary key for conflict handling
    pk_column = _get_primary_key(table_name)

    if pk_column and pk_column in columns:
        # Use upsert (INSERT ... ON CONFLICT DO UPDATE)
        update_cols = [f"{col} = EXCLUDED.{col}" for col in columns if col != pk_column]
        query = f"""
            INSERT INTO {table_name} ({column_list})
            VALUES ({placeholders})
            ON CONFLICT ({pk_column}) DO UPDATE SET {', '.join(update_cols)}
        """
    else:
        # Simple insert
        query = f"INSERT INTO {table_name} ({column_list}) VALUES ({placeholders})"

    # Insert rows in batches
    batch_size = 100
    inserted = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        for row in batch:
            values = tuple(_convert_value(row[col], col) for col in columns)
            try:
                postgres_db.execute(query, values)
            except Exception as e:
                logger.error(f"Error inserting row into {table_name}: {e}")
                postgres_db.rollback()
                raise
        postgres_db.commit()
        inserted += len(batch)

    logger.info(f"Migrated {inserted} rows to {table_name}")


def _get_primary_key(table_name: str) -> str:
    """Get the primary key column for a table."""
    pk_map = {
        'servers': 'id',
        's3_configs': 'id',
        'db_configs': 'id',
        'jobs': 'id',
        'history': 'id',
        'notification_channels': 'id',
        'audit_log': 'id',
        'scheduler_lock': 'id',
        'scheduled_jobs': 'job_id'
    }
    return pk_map.get(table_name)


def _convert_value(value: Any, column_name: str) -> Any:
    """Convert a value for PostgreSQL compatibility."""
    if value is None:
        return None

    # Convert SQLite INTEGER boolean to Python bool
    if column_name in ('enabled', 'schedule_enabled', 'notify_on_success', 'notify_on_failure', 'is_active'):
        return bool(value) if value is not None else None

    return value


def migrate_json_to_database(data_dir: str, db) -> None:
    """
    Migrate legacy JSON files to database.
    This is kept for backward compatibility with older installations.

    Args:
        data_dir: Directory containing JSON files
        db: Database backend instance
    """
    from datetime import datetime

    data_path = Path(data_dir)

    # Check if migration is needed
    has_data = False
    for table in ['servers', 's3_configs', 'db_configs', 'jobs', 'history']:
        try:
            count = db.fetchone(f'SELECT COUNT(*) as count FROM {table}')
            if count and count['count'] > 0:
                has_data = True
                break
        except Exception:
            pass

    if has_data:
        logger.debug("Database already has data, skipping JSON migration")
        return

    logger.info("Migrating JSON data to database...")

    # Migrate servers
    servers_file = data_path / 'servers.json'
    if servers_file.exists():
        try:
            with open(servers_file) as f:
                servers = json.load(f)
            for s in servers:
                db.execute('''
                    INSERT INTO servers (id, name, host, connection_type, ssh_port, ssh_user, ssh_key, agent_port, agent_api_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (s['id'], s['name'], s['host'], s.get('connection_type', 'ssh'),
                      s.get('ssh_port', 22), s.get('ssh_user'), s.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
                      s.get('agent_port', 8090), s.get('agent_api_key'),
                      s.get('created_at', datetime.now().isoformat()), s.get('updated_at')))
            db.commit()
            logger.info(f"  Migrated {len(servers)} servers")
        except Exception as e:
            logger.error(f"  Error migrating servers: {e}")

    # Migrate S3 configs
    s3_file = data_path / 's3_configs.json'
    if s3_file.exists():
        try:
            with open(s3_file) as f:
                configs = json.load(f)
            for c in configs:
                db.execute('''
                    INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (c['id'], c['name'], c['endpoint'], c['bucket'], c['access_key'], c['secret_key'],
                      c.get('region', ''), c.get('created_at', datetime.now().isoformat()), c.get('updated_at')))
            db.commit()
            logger.info(f"  Migrated {len(configs)} S3 configs")
        except Exception as e:
            logger.error(f"  Error migrating S3 configs: {e}")

    # Migrate database configs
    db_file = data_path / 'db_configs.json'
    if db_file.exists():
        try:
            with open(db_file) as f:
                configs = json.load(f)
            for c in configs:
                db.execute('''
                    INSERT INTO db_configs (id, name, type, host, port, username, password, databases, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (c['id'], c['name'], c.get('type', 'mysql'), c['host'], c.get('port', 3306),
                      c['username'], c['password'], c.get('databases', '*'),
                      c.get('created_at', datetime.now().isoformat()), c.get('updated_at')))
            db.commit()
            logger.info(f"  Migrated {len(configs)} database configs")
        except Exception as e:
            logger.error(f"  Error migrating database configs: {e}")

    # Migrate jobs
    jobs_file = data_path / 'jobs.json'
    if jobs_file.exists():
        try:
            with open(jobs_file) as f:
                jobs = json.load(f)
            for job_id, j in jobs.items():
                db.execute('''
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
            db.commit()
            logger.info(f"  Migrated {len(jobs)} jobs")
        except Exception as e:
            logger.error(f"  Error migrating jobs: {e}")

    # Migrate history
    history_file = data_path / 'history.json'
    if history_file.exists():
        try:
            with open(history_file) as f:
                history = json.load(f)
            for h in history:
                db.execute('''
                    INSERT INTO history (timestamp, job_id, job_name, status, message, duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (h['timestamp'], h['job_id'], h['job_name'], h['status'], h.get('message', ''), h.get('duration', 0)))
            db.commit()
            logger.info(f"  Migrated {len(history)} history entries")
        except Exception as e:
            logger.error(f"  Error migrating history: {e}")

    logger.info("JSON migration complete!")


if __name__ == '__main__':
    """Command-line migration tool."""
    import argparse

    parser = argparse.ArgumentParser(description='Migrate BackupX database')
    parser.add_argument('--source-sqlite', required=True, help='Path to source SQLite database')
    parser.add_argument('--target-host', default='localhost', help='PostgreSQL host')
    parser.add_argument('--target-port', type=int, default=5432, help='PostgreSQL port')
    parser.add_argument('--target-database', default='backupx', help='PostgreSQL database name')
    parser.add_argument('--target-user', required=True, help='PostgreSQL user')
    parser.add_argument('--target-password', default='', help='PostgreSQL password')

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    migrate_sqlite_to_postgres(
        args.source_sqlite,
        {
            'host': args.target_host,
            'port': args.target_port,
            'database': args.target_database,
            'user': args.target_user,
            'password': args.target_password
        }
    )

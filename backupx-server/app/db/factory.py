"""
Database backend factory.
Creates appropriate database backend based on configuration.
"""

import os
import logging
from typing import Optional
from pathlib import Path

from .base import DatabaseBackend
from .sqlite import SQLiteBackend

logger = logging.getLogger(__name__)

# Global database instance
_db_instance: Optional[DatabaseBackend] = None


def create_database_backend() -> DatabaseBackend:
    """
    Create a database backend based on environment configuration.

    Environment variables:
        DATABASE_TYPE: 'sqlite' (default) or 'postgresql'

        For SQLite:
            DATABASE_PATH: Path to SQLite file (default: /app/data/backupx.db)

        For PostgreSQL:
            DATABASE_HOST: Host (default: localhost)
            DATABASE_PORT: Port (default: 5432)
            DATABASE_NAME: Database name (default: backupx)
            DATABASE_USER: Username (required)
            DATABASE_PASSWORD: Password (required)
            DATABASE_POOL_MIN: Min pool size (default: 2)
            DATABASE_POOL_MAX: Max pool size (default: 10)
            DATABASE_SSL_MODE: SSL mode (default: prefer)

    Returns:
        DatabaseBackend instance
    """
    db_type = os.environ.get('DATABASE_TYPE', 'sqlite').lower()

    if db_type == 'postgresql' or db_type == 'postgres':
        from .postgres import PostgresBackend

        host = os.environ.get('DATABASE_HOST', 'localhost')
        port = int(os.environ.get('DATABASE_PORT', 5432))
        database = os.environ.get('DATABASE_NAME', 'backupx')
        user = os.environ.get('DATABASE_USER')
        password = os.environ.get('DATABASE_PASSWORD', '')
        pool_min = int(os.environ.get('DATABASE_POOL_MIN', 2))
        pool_max = int(os.environ.get('DATABASE_POOL_MAX', 10))
        ssl_mode = os.environ.get('DATABASE_SSL_MODE', 'prefer')

        if not user:
            raise ValueError("DATABASE_USER is required for PostgreSQL")

        logger.info(f"Creating PostgreSQL backend: {user}@{host}:{port}/{database}")
        return PostgresBackend(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            pool_min=pool_min,
            pool_max=pool_max,
            ssl_mode=ssl_mode
        )
    else:
        # Default to SQLite
        db_path = os.environ.get('DATABASE_PATH', '/app/data/backupx.db')

        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Creating SQLite backend: {db_path}")
        return SQLiteBackend(db_path)


def get_database() -> DatabaseBackend:
    """
    Get the global database instance.
    Creates it if it doesn't exist.

    Returns:
        DatabaseBackend instance
    """
    global _db_instance
    if _db_instance is None:
        _db_instance = create_database_backend()
    return _db_instance


def init_database() -> DatabaseBackend:
    """
    Initialize the database (create schema, run migrations).

    Returns:
        DatabaseBackend instance
    """
    db = get_database()
    db.init_schema()

    # Run migrations if the backend supports it
    if hasattr(db, 'migrate_schema'):
        db.migrate_schema()

    return db


def close_database() -> None:
    """Close the global database instance."""
    global _db_instance
    if _db_instance is not None:
        if hasattr(_db_instance, 'close_all'):
            _db_instance.close_all()
        else:
            _db_instance.close()
        _db_instance = None
        logger.info("Database connection closed")


def reset_database() -> None:
    """
    Reset the global database instance.
    Used primarily for testing.
    """
    global _db_instance
    _db_instance = None

"""
Database abstraction layer for BackupX.
Supports SQLite and PostgreSQL backends.
"""

from .factory import get_database, init_database, close_database
from .base import DatabaseBackend

__all__ = ['get_database', 'init_database', 'close_database', 'DatabaseBackend']

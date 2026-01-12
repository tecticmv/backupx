"""
Abstract base class for database backends.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union
from contextlib import contextmanager


class DatabaseBackend(ABC):
    """Abstract base class defining the database interface."""

    @abstractmethod
    def get_connection(self):
        """Get a database connection."""
        pass

    @abstractmethod
    def close(self):
        """Close all connections and cleanup resources."""
        pass

    @abstractmethod
    def execute(self, query: str, params: Optional[Tuple] = None) -> Any:
        """
        Execute a query without returning results.
        Used for INSERT, UPDATE, DELETE operations.

        Args:
            query: SQL query with placeholders
            params: Query parameters

        Returns:
            Cursor or affected row count depending on backend
        """
        pass

    @abstractmethod
    def executemany(self, query: str, params_list: List[Tuple]) -> Any:
        """
        Execute a query multiple times with different parameters.

        Args:
            query: SQL query with placeholders
            params_list: List of parameter tuples
        """
        pass

    @abstractmethod
    def executescript(self, script: str) -> None:
        """
        Execute multiple SQL statements (DDL).

        Args:
            script: Multi-statement SQL script
        """
        pass

    @abstractmethod
    def fetchone(self, query: str, params: Optional[Tuple] = None) -> Optional[Dict[str, Any]]:
        """
        Execute a query and fetch one result as a dictionary.

        Args:
            query: SQL query with placeholders
            params: Query parameters

        Returns:
            Dictionary with column names as keys, or None
        """
        pass

    @abstractmethod
    def fetchall(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """
        Execute a query and fetch all results as dictionaries.

        Args:
            query: SQL query with placeholders
            params: Query parameters

        Returns:
            List of dictionaries with column names as keys
        """
        pass

    @abstractmethod
    def commit(self) -> None:
        """Commit the current transaction."""
        pass

    @abstractmethod
    def rollback(self) -> None:
        """Rollback the current transaction."""
        pass

    @abstractmethod
    def init_schema(self) -> None:
        """Initialize database schema (create tables, indexes)."""
        pass

    @abstractmethod
    def get_table_columns(self, table_name: str) -> List[str]:
        """
        Get list of column names for a table.
        Used for schema migrations.

        Args:
            table_name: Name of the table

        Returns:
            List of column names
        """
        pass

    @abstractmethod
    def add_column(self, table_name: str, column_name: str, column_type: str, default: Optional[str] = None) -> None:
        """
        Add a column to an existing table.

        Args:
            table_name: Name of the table
            column_name: Name of the new column
            column_type: SQL type for the column
            default: Default value (optional)
        """
        pass

    @contextmanager
    def transaction(self):
        """
        Context manager for transactions.
        Commits on success, rolls back on exception.
        """
        try:
            yield self
            self.commit()
        except Exception:
            self.rollback()
            raise

    @property
    @abstractmethod
    def placeholder(self) -> str:
        """
        Get the parameter placeholder for this backend.
        SQLite uses '?', PostgreSQL uses '%s'.
        """
        pass

    def convert_query(self, query: str) -> str:
        """
        Convert a query with '?' placeholders to the backend's format.
        Override in backends that use different placeholders.

        Args:
            query: SQL query with '?' placeholders

        Returns:
            Query with backend-specific placeholders
        """
        return query

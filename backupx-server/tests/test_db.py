"""
Tests for database abstraction layer
"""
import os
import tempfile
import pytest

# Set environment before imports
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-32chars!')


class TestSQLiteBackend:
    """Test SQLite database backend"""

    def test_init_creates_tables(self):
        """Test that init_schema creates required tables"""
        from app.db.sqlite import SQLiteBackend

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            db = SQLiteBackend(db_path)
            db.init_schema()

            # Check that tables exist
            tables = db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            table_names = [t['name'] for t in tables]

            assert 'jobs' in table_names
            assert 'servers' in table_names
            assert 's3_configs' in table_names
            assert 'db_configs' in table_names
            assert 'history' in table_names
            assert 'notification_channels' in table_names
            assert 'audit_log' in table_names

            db.close()
        finally:
            os.unlink(db_path)

    def test_execute_and_fetch(self):
        """Test execute and fetch operations"""
        from app.db.sqlite import SQLiteBackend

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            db = SQLiteBackend(db_path)
            db.init_schema()

            # Insert a server
            db.execute(
                '''INSERT INTO servers (id, name, host, connection_type)
                   VALUES (?, ?, ?, ?)''',
                ('test-1', 'Test Server', '192.168.1.1', 'ssh')
            )
            db.commit()

            # Fetch the server
            server = db.fetchone(
                'SELECT * FROM servers WHERE id = ?', ('test-1',)
            )
            assert server is not None
            assert server['name'] == 'Test Server'
            assert server['host'] == '192.168.1.1'

            # Fetch all servers
            servers = db.fetchall('SELECT * FROM servers')
            assert len(servers) == 1

            db.close()
        finally:
            os.unlink(db_path)

    def test_placeholder_property(self):
        """Test that SQLite uses ? placeholder"""
        from app.db.sqlite import SQLiteBackend

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            db = SQLiteBackend(db_path)
            assert db.placeholder == '?'
            db.close()
        finally:
            os.unlink(db_path)

    def test_get_table_columns(self):
        """Test getting table column information"""
        from app.db.sqlite import SQLiteBackend

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            db = SQLiteBackend(db_path)
            db.init_schema()

            columns = db.get_table_columns('servers')
            column_names = [c['name'] for c in columns]

            assert 'id' in column_names
            assert 'name' in column_names
            assert 'host' in column_names

            db.close()
        finally:
            os.unlink(db_path)

    def test_add_column(self):
        """Test adding a column to a table"""
        from app.db.sqlite import SQLiteBackend

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            db = SQLiteBackend(db_path)
            db.init_schema()

            # Add a new column
            db.add_column('servers', 'test_column', 'TEXT')

            # Verify column exists
            columns = db.get_table_columns('servers')
            column_names = [c['name'] for c in columns]
            assert 'test_column' in column_names

            db.close()
        finally:
            os.unlink(db_path)


class TestDatabaseFactory:
    """Test database factory"""

    def test_factory_creates_sqlite_by_default(self):
        """Test that factory creates SQLite backend by default"""
        from app.db.factory import init_database, get_database, close_database
        from app.db.sqlite import SQLiteBackend

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            # Set environment for SQLite
            old_type = os.environ.get('DATABASE_TYPE')
            old_path = os.environ.get('DATABASE_PATH')
            os.environ['DATABASE_TYPE'] = 'sqlite'
            os.environ['DATABASE_PATH'] = db_path

            db = init_database()
            assert isinstance(db, SQLiteBackend)

            # get_database should return same instance
            db2 = get_database()
            assert db is db2

            close_database()

            # Restore environment
            if old_type:
                os.environ['DATABASE_TYPE'] = old_type
            else:
                os.environ.pop('DATABASE_TYPE', None)
            if old_path:
                os.environ['DATABASE_PATH'] = old_path
            else:
                os.environ.pop('DATABASE_PATH', None)
        finally:
            try:
                os.unlink(db_path)
            except:
                pass

    def test_factory_raises_for_invalid_type(self):
        """Test that factory raises error for invalid database type"""
        from app.db.factory import init_database, close_database

        old_type = os.environ.get('DATABASE_TYPE')
        os.environ['DATABASE_TYPE'] = 'invalid_db_type'

        try:
            with pytest.raises(ValueError):
                init_database()
        finally:
            if old_type:
                os.environ['DATABASE_TYPE'] = old_type
            else:
                os.environ.pop('DATABASE_TYPE', None)


class TestMigration:
    """Test migration utilities"""

    def test_value_conversion(self):
        """Test value conversion for PostgreSQL compatibility"""
        from app.db.migrate import _convert_value

        # Boolean conversion
        assert _convert_value(1, 'enabled') == True
        assert _convert_value(0, 'enabled') == False
        assert _convert_value(None, 'enabled') is None

        # Regular values pass through
        assert _convert_value('test', 'name') == 'test'
        assert _convert_value(123, 'port') == 123

    def test_primary_key_mapping(self):
        """Test primary key mapping for tables"""
        from app.db.migrate import _get_primary_key

        assert _get_primary_key('servers') == 'id'
        assert _get_primary_key('jobs') == 'id'
        assert _get_primary_key('scheduled_jobs') == 'job_id'
        assert _get_primary_key('unknown_table') is None

"""
Tests for session configuration (Redis and filesystem)
"""
import os
import pytest
from unittest.mock import MagicMock, patch

# Set environment before imports
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-32chars!')


class TestSessionConfiguration:
    """Test session configuration"""

    def test_filesystem_session_default(self):
        """Test filesystem session is used by default"""
        from app.session import get_rate_limiter_storage_uri

        # Clear any redis config
        old_type = os.environ.get('SESSION_TYPE')
        old_host = os.environ.get('REDIS_HOST')
        os.environ.pop('SESSION_TYPE', None)
        os.environ.pop('REDIS_HOST', None)

        try:
            uri = get_rate_limiter_storage_uri()
            assert uri == 'memory://'
        finally:
            if old_type:
                os.environ['SESSION_TYPE'] = old_type
            if old_host:
                os.environ['REDIS_HOST'] = old_host

    def test_redis_rate_limiter_uri(self):
        """Test Redis rate limiter URI when configured"""
        from app.session import get_rate_limiter_storage_uri

        old_type = os.environ.get('SESSION_TYPE')
        old_host = os.environ.get('REDIS_HOST')
        old_port = os.environ.get('REDIS_PORT')
        old_db = os.environ.get('REDIS_RATE_LIMIT_DB')

        os.environ['SESSION_TYPE'] = 'redis'
        os.environ['REDIS_HOST'] = 'localhost'
        os.environ['REDIS_PORT'] = '6379'
        os.environ['REDIS_RATE_LIMIT_DB'] = '1'

        try:
            uri = get_rate_limiter_storage_uri()
            assert uri == 'redis://localhost:6379/1'
        finally:
            if old_type:
                os.environ['SESSION_TYPE'] = old_type
            else:
                os.environ.pop('SESSION_TYPE', None)
            if old_host:
                os.environ['REDIS_HOST'] = old_host
            else:
                os.environ.pop('REDIS_HOST', None)
            if old_port:
                os.environ['REDIS_PORT'] = old_port
            else:
                os.environ.pop('REDIS_PORT', None)
            if old_db:
                os.environ['REDIS_RATE_LIMIT_DB'] = old_db
            else:
                os.environ.pop('REDIS_RATE_LIMIT_DB', None)

    def test_redis_rate_limiter_with_password(self):
        """Test Redis rate limiter URI with password"""
        from app.session import get_rate_limiter_storage_uri

        old_vals = {}
        for key in ['SESSION_TYPE', 'REDIS_HOST', 'REDIS_PORT', 'REDIS_PASSWORD', 'REDIS_RATE_LIMIT_DB']:
            old_vals[key] = os.environ.get(key)

        os.environ['SESSION_TYPE'] = 'redis'
        os.environ['REDIS_HOST'] = 'redis.example.com'
        os.environ['REDIS_PORT'] = '6380'
        os.environ['REDIS_PASSWORD'] = 'secret123'
        os.environ['REDIS_RATE_LIMIT_DB'] = '2'

        try:
            uri = get_rate_limiter_storage_uri()
            assert uri == 'redis://:secret123@redis.example.com:6380/2'
        finally:
            for key, val in old_vals.items():
                if val:
                    os.environ[key] = val
                else:
                    os.environ.pop(key, None)


class TestRedisClient:
    """Test Redis client configuration"""

    def test_get_redis_client_when_not_configured(self):
        """Test that get_redis_client returns None when not configured"""
        from app.session import get_redis_client

        old_type = os.environ.get('SESSION_TYPE')
        os.environ.pop('SESSION_TYPE', None)

        try:
            client = get_redis_client()
            assert client is None
        finally:
            if old_type:
                os.environ['SESSION_TYPE'] = old_type

    @patch('app.session.redis')
    def test_get_redis_client_when_configured(self, mock_redis):
        """Test that get_redis_client returns Redis instance when configured"""
        from app.session import get_redis_client

        old_vals = {}
        for key in ['SESSION_TYPE', 'REDIS_HOST', 'REDIS_PORT']:
            old_vals[key] = os.environ.get(key)

        os.environ['SESSION_TYPE'] = 'redis'
        os.environ['REDIS_HOST'] = 'localhost'
        os.environ['REDIS_PORT'] = '6379'

        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        try:
            client = get_redis_client()
            assert client is not None
            mock_redis.Redis.assert_called_once()
        finally:
            for key, val in old_vals.items():
                if val:
                    os.environ[key] = val
                else:
                    os.environ.pop(key, None)


class TestConfigureSession:
    """Test session configuration for Flask app"""

    def test_configure_filesystem_session(self):
        """Test configuring filesystem session"""
        from app.session import configure_session

        old_type = os.environ.get('SESSION_TYPE')
        os.environ['SESSION_TYPE'] = 'filesystem'

        mock_app = MagicMock()
        mock_app.config = {}

        try:
            configure_session(mock_app)
            assert mock_app.config.get('SESSION_TYPE') == 'filesystem'
        finally:
            if old_type:
                os.environ['SESSION_TYPE'] = old_type
            else:
                os.environ.pop('SESSION_TYPE', None)

    @patch('app.session.redis')
    @patch('app.session.Session')
    def test_configure_redis_session(self, mock_session_class, mock_redis):
        """Test configuring Redis session"""
        from app.session import configure_session

        old_vals = {}
        for key in ['SESSION_TYPE', 'REDIS_HOST', 'REDIS_PORT', 'REDIS_SESSION_DB']:
            old_vals[key] = os.environ.get(key)

        os.environ['SESSION_TYPE'] = 'redis'
        os.environ['REDIS_HOST'] = 'localhost'
        os.environ['REDIS_PORT'] = '6379'
        os.environ['REDIS_SESSION_DB'] = '0'

        mock_app = MagicMock()
        mock_app.config = {}

        mock_redis_instance = MagicMock()
        mock_redis.Redis.return_value = mock_redis_instance

        try:
            configure_session(mock_app)
            assert mock_app.config.get('SESSION_TYPE') == 'redis'
            assert mock_app.config.get('SESSION_REDIS') is mock_redis_instance
        finally:
            for key, val in old_vals.items():
                if val:
                    os.environ[key] = val
                else:
                    os.environ.pop(key, None)

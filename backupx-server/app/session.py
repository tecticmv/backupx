"""
Session configuration for BackupX.
Supports filesystem (default) and Redis-backed sessions for horizontal scaling.
"""

import os
import logging

logger = logging.getLogger(__name__)


def configure_session(app):
    """
    Configure Flask session storage.

    Environment variables:
        SESSION_TYPE: 'filesystem' (default) or 'redis'

        For Redis:
            REDIS_HOST: Redis host (default: localhost)
            REDIS_PORT: Redis port (default: 6379)
            REDIS_PASSWORD: Redis password (optional)
            REDIS_SSL: Enable SSL (default: false)
            REDIS_SESSION_DB: Redis DB number for sessions (default: 0)

    Args:
        app: Flask application instance
    """
    session_type = os.environ.get('SESSION_TYPE', 'filesystem').lower()

    if session_type == 'redis':
        _configure_redis_session(app)
    else:
        _configure_filesystem_session(app)


def _configure_filesystem_session(app):
    """Configure filesystem-based sessions (default)."""
    try:
        from flask_session import Session

        session_dir = os.environ.get('SESSION_FILE_DIR', '/app/data/sessions')
        os.makedirs(session_dir, exist_ok=True)

        app.config['SESSION_TYPE'] = 'filesystem'
        app.config['SESSION_FILE_DIR'] = session_dir
        app.config['SESSION_PERMANENT'] = True
        app.config['SESSION_USE_SIGNER'] = True
        app.config['SESSION_KEY_PREFIX'] = 'backupx:'

        Session(app)
        logger.info(f"Filesystem session storage configured: {session_dir}")

    except ImportError:
        # flask-session not installed, use default Flask sessions
        logger.info("Using default Flask session storage (flask-session not installed)")


def _configure_redis_session(app):
    """Configure Redis-backed sessions for horizontal scaling."""
    try:
        from flask_session import Session
        import redis

        redis_host = os.environ.get('REDIS_HOST', 'localhost')
        redis_port = int(os.environ.get('REDIS_PORT', 6379))
        redis_password = os.environ.get('REDIS_PASSWORD', None)
        redis_ssl = os.environ.get('REDIS_SSL', 'false').lower() in ('true', '1', 'yes')
        redis_db = int(os.environ.get('REDIS_SESSION_DB', 0))

        # Create Redis client
        redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password if redis_password else None,
            db=redis_db,
            ssl=redis_ssl,
            decode_responses=False  # Sessions need binary data
        )

        # Test connection
        redis_client.ping()

        app.config['SESSION_TYPE'] = 'redis'
        app.config['SESSION_REDIS'] = redis_client
        app.config['SESSION_PERMANENT'] = True
        app.config['SESSION_USE_SIGNER'] = True
        app.config['SESSION_KEY_PREFIX'] = 'backupx:session:'

        Session(app)
        logger.info(f"Redis session storage configured: {redis_host}:{redis_port}/{redis_db}")

    except ImportError:
        logger.warning("redis or flask-session not installed, falling back to filesystem sessions")
        _configure_filesystem_session(app)

    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}, falling back to filesystem sessions")
        _configure_filesystem_session(app)


def get_redis_client():
    """
    Get a Redis client for general use (rate limiting, caching, etc.).

    Returns:
        Redis client instance or None if not configured
    """
    redis_host = os.environ.get('REDIS_HOST')
    if not redis_host:
        return None

    try:
        import redis

        redis_port = int(os.environ.get('REDIS_PORT', 6379))
        redis_password = os.environ.get('REDIS_PASSWORD', None)
        redis_ssl = os.environ.get('REDIS_SSL', 'false').lower() in ('true', '1', 'yes')
        redis_db = int(os.environ.get('REDIS_RATE_LIMIT_DB', 1))

        client = redis.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password if redis_password else None,
            db=redis_db,
            ssl=redis_ssl,
            decode_responses=True
        )

        # Test connection
        client.ping()
        return client

    except ImportError:
        logger.warning("redis package not installed")
        return None

    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        return None


def get_rate_limiter_storage_uri():
    """
    Get the storage URI for Flask-Limiter.

    Returns:
        Storage URI string ('memory://' or 'redis://...')
    """
    redis_host = os.environ.get('REDIS_HOST')
    if not redis_host:
        return "memory://"

    redis_port = os.environ.get('REDIS_PORT', 6379)
    redis_password = os.environ.get('REDIS_PASSWORD', '')
    redis_db = os.environ.get('REDIS_RATE_LIMIT_DB', 1)

    if redis_password:
        return f"redis://:{redis_password}@{redis_host}:{redis_port}/{redis_db}"
    else:
        return f"redis://{redis_host}:{redis_port}/{redis_db}"

"""
Pytest fixtures for BackupX Server tests
"""
import os
import sys
import tempfile
import pytest

# Add the app directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Set test environment variables before importing app
os.environ['SECRET_KEY'] = 'test-secret-key-for-testing-only-32chars!'
os.environ['ADMIN_USERNAME'] = 'testadmin'
os.environ['ADMIN_PASSWORD'] = 'testpassword123'


@pytest.fixture
def app():
    """Create application for testing"""
    from app.main import app as flask_app

    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    flask_app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
    })

    yield flask_app

    # Cleanup
    try:
        os.unlink(db_path)
    except:
        pass


@pytest.fixture
def client(app):
    """Create test client"""
    return app.test_client()


@pytest.fixture
def authenticated_client(client):
    """Create an authenticated test client"""
    response = client.post('/api/auth/login',
        json={'username': 'testadmin', 'password': 'testpassword123'},
        content_type='application/json'
    )
    assert response.status_code == 200
    return client

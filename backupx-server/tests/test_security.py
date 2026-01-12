"""
Security-related tests for BackupX Server
"""
import os
import pytest


class TestAuthentication:
    """Test authentication functionality"""

    def test_login_success(self, client):
        """Test successful login with correct credentials"""
        response = client.post('/api/auth/login',
            json={'username': 'testadmin', 'password': 'testpassword123'},
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert 'user' in data
        assert data['user']['username'] == 'testadmin'

    def test_login_failure_wrong_password(self, client):
        """Test login failure with wrong password"""
        response = client.post('/api/auth/login',
            json={'username': 'testadmin', 'password': 'wrongpassword'},
            content_type='application/json'
        )
        assert response.status_code == 401
        data = response.get_json()
        assert 'error' in data

    def test_login_failure_wrong_username(self, client):
        """Test login failure with wrong username"""
        response = client.post('/api/auth/login',
            json={'username': 'wronguser', 'password': 'testpassword123'},
            content_type='application/json'
        )
        assert response.status_code == 401

    def test_login_no_data(self, client):
        """Test login with no data provided"""
        response = client.post('/api/auth/login',
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_protected_route_requires_auth(self, client):
        """Test that protected routes require authentication"""
        response = client.get('/api/jobs')
        # Should redirect to login or return 401
        assert response.status_code in [401, 302]

    def test_logout(self, authenticated_client):
        """Test logout functionality"""
        response = authenticated_client.post('/api/auth/logout')
        assert response.status_code == 200

    def test_auth_me_authenticated(self, authenticated_client):
        """Test /api/auth/me returns user info when authenticated"""
        response = authenticated_client.get('/api/auth/me')
        assert response.status_code == 200
        data = response.get_json()
        assert data['username'] == 'testadmin'

    def test_auth_me_unauthenticated(self, client):
        """Test /api/auth/me returns error when not authenticated"""
        response = client.get('/api/auth/me')
        assert response.status_code == 401


class TestCredentialEncryption:
    """Test credential encryption functionality"""

    def test_encrypt_decrypt_roundtrip(self):
        """Test that encryption and decryption work correctly"""
        # Import after env vars are set
        from app.main import encrypt_credential, decrypt_credential

        original = "my-secret-password-123"
        encrypted = encrypt_credential(original)

        # Encrypted value should be different from original
        assert encrypted != original

        # Decryption should return original
        decrypted = decrypt_credential(encrypted)
        assert decrypted == original

    def test_encrypt_empty_string(self):
        """Test encryption of empty string"""
        from app.main import encrypt_credential, decrypt_credential

        assert encrypt_credential('') == ''
        assert decrypt_credential('') == ''

    def test_decrypt_plaintext_fallback(self):
        """Test that decryption falls back to plaintext for legacy values"""
        from app.main import decrypt_credential

        # Non-encrypted values should be returned as-is (legacy support)
        plaintext = "legacy-unencrypted-password"
        result = decrypt_credential(plaintext)
        assert result == plaintext

    def test_is_encrypted_detection(self):
        """Test detection of encrypted values"""
        from app.main import encrypt_credential, is_encrypted

        encrypted = encrypt_credential("test-password")
        assert is_encrypted(encrypted) == True
        assert is_encrypted("plaintext-value") == False
        assert is_encrypted("") == False
        assert is_encrypted(None) == False


class TestPasswordHashing:
    """Test password hashing functionality"""

    def test_password_hash_is_generated(self):
        """Test that admin password hash is generated"""
        from app.main import get_admin_password_hash
        from werkzeug.security import check_password_hash

        password_hash = get_admin_password_hash()
        assert password_hash is not None
        assert password_hash != 'testpassword123'  # Should not be plaintext
        assert check_password_hash(password_hash, 'testpassword123')

    def test_password_hash_is_cached(self):
        """Test that password hash is cached (same instance returned)"""
        from app.main import get_admin_password_hash

        hash1 = get_admin_password_hash()
        hash2 = get_admin_password_hash()
        assert hash1 == hash2


class TestHealthEndpoint:
    """Test health check endpoint"""

    def test_health_check(self, client):
        """Test health endpoint returns OK"""
        response = client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'

    def test_health_no_auth_required(self, client):
        """Test health endpoint doesn't require authentication"""
        response = client.get('/health')
        assert response.status_code == 200

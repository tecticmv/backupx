"""
Input validation tests for BackupX Server
"""
import pytest


class TestInputValidation:
    """Test input validation functions"""

    def test_validate_hostname_valid(self):
        """Test hostname validation with valid inputs"""
        from app.main import validate_hostname

        assert validate_hostname('example.com') == True
        assert validate_hostname('sub.example.com') == True
        assert validate_hostname('192.168.1.1') == True
        assert validate_hostname('10.0.0.1') == True
        assert validate_hostname('localhost') == True

    def test_validate_hostname_invalid(self):
        """Test hostname validation with invalid inputs"""
        from app.main import validate_hostname

        assert validate_hostname('') == False
        assert validate_hostname(None) == False
        assert validate_hostname('a' * 256) == False  # Too long
        assert validate_hostname('-invalid.com') == False

    def test_validate_port_valid(self):
        """Test port validation with valid inputs"""
        from app.main import validate_port

        assert validate_port(22) == True
        assert validate_port(80) == True
        assert validate_port(443) == True
        assert validate_port(8080) == True
        assert validate_port(1) == True
        assert validate_port(65535) == True

    def test_validate_port_invalid(self):
        """Test port validation with invalid inputs"""
        from app.main import validate_port

        assert validate_port(0) == False
        assert validate_port(-1) == False
        assert validate_port(65536) == False
        assert validate_port('22') == False  # String, not int

    def test_validate_path_valid(self):
        """Test path validation with valid inputs"""
        from app.main import validate_path

        assert validate_path('/home/user') == True
        assert validate_path('/var/log') == True
        assert validate_path('/') == True

    def test_validate_path_invalid(self):
        """Test path validation with invalid inputs"""
        from app.main import validate_path

        assert validate_path('') == False
        assert validate_path('../etc/passwd') == False  # Path traversal
        assert validate_path('/home/../etc') == False
        assert validate_path('~/.ssh') == False
        assert validate_path('relative/path') == False

    def test_validate_cron_valid(self):
        """Test cron expression validation with valid inputs"""
        from app.main import validate_cron

        assert validate_cron('0 2 * * *') == True
        assert validate_cron('*/5 * * * *') == True
        assert validate_cron('0 0 1 1 *') == True
        assert validate_cron('0,30 * * * *') == True

    def test_validate_cron_invalid(self):
        """Test cron expression validation with invalid inputs"""
        from app.main import validate_cron

        assert validate_cron('') == False
        assert validate_cron('invalid') == False
        assert validate_cron('* * *') == False  # Too few fields
        assert validate_cron('* * * * * *') == False  # Too many fields

    def test_validate_s3_endpoint_valid(self):
        """Test S3 endpoint validation with valid inputs"""
        from app.main import validate_s3_endpoint

        assert validate_s3_endpoint('s3.amazonaws.com') == True
        assert validate_s3_endpoint('minio.example.com') == True
        assert validate_s3_endpoint('storage.example.com:9000') == True

    def test_validate_s3_endpoint_invalid(self):
        """Test S3 endpoint validation with invalid inputs"""
        from app.main import validate_s3_endpoint

        assert validate_s3_endpoint('') == False
        assert validate_s3_endpoint('-invalid.com') == False

    def test_validate_bucket_name_valid(self):
        """Test bucket name validation with valid inputs"""
        from app.main import validate_bucket_name

        assert validate_bucket_name('my-bucket') == True
        assert validate_bucket_name('backup-2024') == True
        assert validate_bucket_name('abc') == True

    def test_validate_bucket_name_invalid(self):
        """Test bucket name validation with invalid inputs"""
        from app.main import validate_bucket_name

        assert validate_bucket_name('') == False
        assert validate_bucket_name('ab') == False  # Too short
        assert validate_bucket_name('a' * 64) == False  # Too long
        assert validate_bucket_name('MyBucket') == False  # Uppercase not allowed
        assert validate_bucket_name('-bucket') == False  # Can't start with dash

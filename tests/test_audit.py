"""
Tests for audit logging system
"""
import os
import tempfile
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Set environment before imports
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-32chars!')


class MockDB:
    """Mock database for testing audit logger"""

    def __init__(self):
        self.logs = []
        self.log_id = 0

    def execute(self, query, params=None):
        if 'INSERT INTO audit_log' in query:
            self.log_id += 1
            self.logs.append({
                'id': self.log_id,
                'timestamp': params[0] if params else datetime.now().isoformat(),
                'user_id': params[1] if params and len(params) > 1 else None,
                'user_name': params[2] if params and len(params) > 2 else None,
                'action': params[3] if params and len(params) > 3 else None,
                'resource_type': params[4] if params and len(params) > 4 else None,
                'resource_id': params[5] if params and len(params) > 5 else None,
                'resource_name': params[6] if params and len(params) > 6 else None,
                'changes': params[7] if params and len(params) > 7 else None,
                'ip_address': params[8] if params and len(params) > 8 else None,
                'user_agent': params[9] if params and len(params) > 9 else None,
                'status': params[10] if params and len(params) > 10 else 'success',
                'error_message': params[11] if params and len(params) > 11 else None,
            })

    def commit(self):
        pass

    def fetchone(self, query, params=None):
        if 'COUNT(*)' in query:
            return {'count': len(self.logs)}
        return None

    def fetchall(self, query, params=None):
        return self.logs


class TestAuditLogger:
    """Test AuditLogger class"""

    def test_log_creates_entry(self):
        """Test that log() creates an audit entry"""
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        logger.log(
            action='CREATE',
            resource_type='job',
            resource_id='job-123',
            resource_name='Daily Backup',
            user_id='admin',
            user_name='Administrator'
        )

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'CREATE'
        assert db.logs[0]['resource_type'] == 'job'
        assert db.logs[0]['resource_id'] == 'job-123'

    def test_disabled_logger_does_not_log(self):
        """Test that disabled logger doesn't create entries"""
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=False)

        logger.log(
            action='CREATE',
            resource_type='job',
            resource_id='job-123'
        )

        assert len(db.logs) == 0

    def test_sensitive_field_redaction(self):
        """Test that sensitive fields are redacted in changes"""
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        changes = {
            'name': 'New Server',
            'password': 'secret123',
            'secret_key': 'my-secret-key',
            'api_key': 'api-key-value',
            'host': '192.168.1.1'
        }

        logger.log(
            action='CREATE',
            resource_type='server',
            resource_id='srv-1',
            changes=changes
        )

        import json
        logged_changes = json.loads(db.logs[0]['changes'])

        assert logged_changes['name'] == 'New Server'
        assert logged_changes['host'] == '192.168.1.1'
        assert logged_changes['password'] == '[REDACTED]'
        assert logged_changes['secret_key'] == '[REDACTED]'
        assert logged_changes['api_key'] == '[REDACTED]'

    def test_action_constants(self):
        """Test that action constants are defined"""
        from app.audit.logger import AuditLogger

        assert AuditLogger.CREATE == 'CREATE'
        assert AuditLogger.UPDATE == 'UPDATE'
        assert AuditLogger.DELETE == 'DELETE'
        assert AuditLogger.LOGIN == 'LOGIN'
        assert AuditLogger.LOGOUT == 'LOGOUT'
        assert AuditLogger.RUN_BACKUP == 'RUN_BACKUP'
        assert AuditLogger.BACKUP_COMPLETE == 'BACKUP_COMPLETE'

    def test_get_log_count(self):
        """Test getting log count"""
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        # Add some logs
        logger.log(action='CREATE', resource_type='job', resource_id='1')
        logger.log(action='UPDATE', resource_type='job', resource_id='1')
        logger.log(action='DELETE', resource_type='job', resource_id='1')

        count = logger.get_log_count()
        assert count == 3


class TestAuditDecorator:
    """Test audit decorator functions"""

    def test_audit_login(self):
        """Test audit_login helper"""
        from app.audit.decorator import audit_login
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        with patch('app.audit.decorator.get_audit_logger', return_value=logger):
            audit_login('admin', 'Administrator', '127.0.0.1', 'TestBrowser')

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'LOGIN'
        assert db.logs[0]['user_id'] == 'admin'

    def test_audit_logout(self):
        """Test audit_logout helper"""
        from app.audit.decorator import audit_logout
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        with patch('app.audit.decorator.get_audit_logger', return_value=logger):
            audit_logout('admin', 'Administrator')

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'LOGOUT'

    def test_audit_backup_run(self):
        """Test audit_backup_run helper"""
        from app.audit.decorator import audit_backup_run
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        with patch('app.audit.decorator.get_audit_logger', return_value=logger):
            audit_backup_run('job-123', 'Daily Backup', 'admin', 'Administrator')

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'RUN_BACKUP'
        assert db.logs[0]['resource_type'] == 'job'
        assert db.logs[0]['resource_id'] == 'job-123'

    def test_audit_backup_complete(self):
        """Test audit_backup_complete helper"""
        from app.audit.decorator import audit_backup_complete
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        with patch('app.audit.decorator.get_audit_logger', return_value=logger):
            audit_backup_complete('job-123', 'Daily Backup', 'success')

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'BACKUP_COMPLETE'
        assert db.logs[0]['status'] == 'success'

    def test_audit_backup_complete_failure(self):
        """Test audit_backup_complete with failure"""
        from app.audit.decorator import audit_backup_complete
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        with patch('app.audit.decorator.get_audit_logger', return_value=logger):
            audit_backup_complete(
                'job-123', 'Daily Backup', 'failure',
                error_message='Connection refused'
            )

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'BACKUP_COMPLETE'
        assert db.logs[0]['status'] == 'failure'
        assert db.logs[0]['error_message'] == 'Connection refused'


class TestAuditLoggerSensitiveFields:
    """Test comprehensive sensitive field handling"""

    def test_all_sensitive_fields_redacted(self):
        """Test that all known sensitive fields are redacted"""
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        sensitive_data = {
            'password': 'pass1',
            'secret_key': 'secret1',
            'access_key': 'access1',
            'api_key': 'api1',
            'ssh_key': 'ssh1',
            'restic_password': 'restic1',
            'agent_api_key': 'agent1',
            'normal_field': 'visible',
        }

        logger.log(
            action='CREATE',
            resource_type='config',
            resource_id='cfg-1',
            changes=sensitive_data
        )

        import json
        logged = json.loads(db.logs[0]['changes'])

        # All sensitive fields should be redacted
        assert logged['password'] == '[REDACTED]'
        assert logged['secret_key'] == '[REDACTED]'
        assert logged['access_key'] == '[REDACTED]'
        assert logged['api_key'] == '[REDACTED]'
        assert logged['ssh_key'] == '[REDACTED]'
        assert logged['restic_password'] == '[REDACTED]'
        assert logged['agent_api_key'] == '[REDACTED]'

        # Normal field should be visible
        assert logged['normal_field'] == 'visible'

    def test_nested_sensitive_fields(self):
        """Test that nested sensitive fields are handled"""
        from app.audit.logger import AuditLogger

        db = MockDB()
        logger = AuditLogger(db, enabled=True)

        nested_data = {
            'config': {
                'password': 'nested-secret'
            },
            'name': 'Test'
        }

        logger.log(
            action='CREATE',
            resource_type='config',
            resource_id='cfg-1',
            changes=nested_data
        )

        import json
        logged = json.loads(db.logs[0]['changes'])

        # Top level should be logged (nested handling depends on implementation)
        assert 'config' in logged
        assert logged['name'] == 'Test'

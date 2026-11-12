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
            new_value=changes
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

        assert AuditLogger.ACTION_CREATE == 'CREATE'
        assert AuditLogger.ACTION_UPDATE == 'UPDATE'
        assert AuditLogger.ACTION_DELETE == 'DELETE'
        assert AuditLogger.ACTION_LOGIN == 'LOGIN'
        assert AuditLogger.ACTION_LOGOUT == 'LOGOUT'
        assert AuditLogger.ACTION_RUN_BACKUP == 'RUN_BACKUP'
        assert AuditLogger.ACTION_BACKUP_COMPLETE == 'BACKUP_COMPLETE'

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
            audit_logout('admin', 'Administrator', '127.0.0.1')

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
            audit_backup_complete('job-123', 'Daily Backup', True, 12.5)

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
            audit_backup_complete('job-123', 'Daily Backup', False, 3.0,
                                  message='Connection refused')

        assert len(db.logs) == 1
        assert db.logs[0]['action'] == 'BACKUP_FAILED'
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
            new_value=sensitive_data
        )

        import json
        logged = json.loads(db.logs[0]['changes'])

        # All sensitive fields should be redacted
        assert logged['password'] == '[REDACTED]'
        assert logged['secret_key'] == '[REDACTED]'
        assert logged['access_key'] == '[REDACTED]'
        assert logged['api_key'] == '[REDACTED]'
        assert logged['ssh_key'] == 'ssh1'  # a key *path*, not a secret
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
            new_value=nested_data
        )

        import json
        logged = json.loads(db.logs[0]['changes'])

        assert logged['config'] == {'password': '[REDACTED]'}
        assert logged['name'] == 'Test'


# --- Datetime-safe serialisation and delete audits (2026-11-12) -----------------

class TestAuditSerialisation:
    """Rows from the DB carry datetimes; their audit entries must not be dropped."""

    def _logger(self):
        from app.audit.logger import AuditLogger
        db = MockDB()
        return AuditLogger(db, enabled=True), db

    def test_delete_with_datetime_row_is_logged(self):
        import json
        from decimal import Decimal
        audit, db = self._logger()
        row = {'id': 'job-1', 'name': 'Files', 'created_at': datetime(2026, 4, 16, 2, 0),
               'size': Decimal('1.5'), 'restic_password': 'pw', 's3_secret_key': 'sk'}
        assert audit.log(action='DELETE', resource_type='job', resource_id='job-1', old_value=row)
        logged = json.loads(db.logs[0]['changes'])['deleted']
        assert logged['created_at'] == '2026-04-16 02:00:00'
        assert logged['size'] == '1.5'
        assert logged['restic_password'] == '[REDACTED]'
        assert logged['s3_secret_key'] == '[REDACTED]'
        assert logged['name'] == 'Files'

    def test_create_and_update_with_datetimes(self):
        import json
        audit, db = self._logger()
        now = datetime(2026, 11, 12, 10, 0)
        audit.log(action='CREATE', resource_type='job', new_value={'name': 'a', 'created_at': now})
        audit.log(action='UPDATE', resource_type='job',
                  old_value={'updated_at': now - timedelta(days=1)}, new_value={'updated_at': now})
        assert json.loads(db.logs[0]['changes'])['created_at'] == str(now)
        assert json.loads(db.logs[1]['changes'])['updated_at']['new'] == str(now)

    def test_unserialisable_details_still_write_the_entry(self, monkeypatch):
        import json
        from app.audit import logger as audit_module
        audit, db = self._logger()
        monkeypatch.setattr(audit_module, '_dumps', lambda v, _real=audit_module._dumps:
                            _real(v) if v == {'error': 'details could not be serialised'}
                            else (_ for _ in ()).throw(TypeError('boom')))
        audit.log(action='DELETE', resource_type='server', resource_id='s1', old_value={'a': 1})
        assert len(db.logs) == 1
        assert json.loads(db.logs[0]['changes']) == {'error': 'details could not be serialised'}

    def test_server_and_channel_secrets_are_redacted(self):
        import json
        audit, db = self._logger()
        audit.log(action='DELETE', resource_type='server', old_value={
            'host': '10.0.0.1', 'ssh_password': 'pw', 'ssh_key_content': 'KEY'})
        audit.log(action='DELETE', resource_type='notification_channel', old_value={
            'name': 'ops', 'type': 'telegram',
            'config': {'bot_token': 't', 'chat_id': '1', 'webhook_url': 'https://x/secret',
                       'headers': {'Authorization': 'Bearer z'}},
            'list': [{'password': 'p', 'name': 'n'}]})
        server = json.loads(db.logs[0]['changes'])['deleted']
        assert server['ssh_password'] == '[REDACTED]' and server['ssh_key_content'] == '[REDACTED]'
        assert server['host'] == '10.0.0.1'
        channel = json.loads(db.logs[1]['changes'])['deleted']
        assert channel['config']['bot_token'] == '[REDACTED]'
        assert channel['config']['webhook_url'] == '[REDACTED]'
        assert channel['config']['headers'] == '[REDACTED]'
        assert channel['config']['chat_id'] == '1'
        assert channel['list'] == [{'password': '[REDACTED]', 'name': 'n'}]


@pytest.fixture
def audited_api(client, monkeypatch):
    """Logged-in client whose audit entries land in a MockDB (no real rows deleted)."""
    import app.main as main
    from app.audit import logger as audit_module
    from app.audit.logger import AuditLogger
    monkeypatch.setattr(main.limiter, 'enabled', False)
    assert client.post('/api/auth/login', json={'username': 'testadmin',
                                                 'password': 'testpassword123'}).status_code == 200
    db = MockDB()
    monkeypatch.setattr(audit_module, '_audit_logger', AuditLogger(db, enabled=True))
    return client, db, main


@pytest.mark.parametrize('url,getter,deleter,rtype,row', [
    ('/api/servers/srv-1', 'get_server', 'delete_server_from_db', 'server',
     {'id': 'srv-1', 'name': 'web', 'host': '10.0.0.5', 'ssh_password': 'pw',
      'created_at': datetime(2026, 1, 1)}),
    ('/api/s3-configs/srv-1', 'get_s3_config', 'delete_s3_config', 's3_config',
     {'id': 'srv-1', 'name': 'R2', 'access_key': 'ak', 'secret_key': 'sk',
      'created_at': datetime(2026, 1, 1)}),
    ('/api/databases/srv-1', 'get_db_config', 'delete_db_config_from_db', 'db_config',
     {'id': 'srv-1', 'name': 'koha', 'password': 'pw', 'created_at': datetime(2026, 1, 1)}),
    ('/api/notifications/srv-1', 'get_notification_channel', 'delete_notification_channel',
     'notification_channel',
     {'id': 'srv-1', 'name': 'ops', 'config': {'bot_token': 't', 'chat_id': '1'},
      'created_at': datetime(2026, 1, 1)}),
])
def test_deletes_are_audited_with_secrets_redacted(audited_api, monkeypatch, url, getter, deleter,
                                                   rtype, row):
    import json
    client, db, main = audited_api
    deleted = []
    monkeypatch.setattr(main, getter, lambda _id: dict(row))
    monkeypatch.setattr(main, deleter, lambda _id: deleted.append(_id))
    monkeypatch.setattr(main, 'load_jobs', lambda: {})
    assert client.delete(url).status_code == 200
    assert deleted == ['srv-1']
    entries = [e for e in db.logs if e['action'] == 'DELETE']
    assert len(entries) == 1
    entry = entries[0]
    assert entry['resource_type'] == rtype and entry['resource_id'] == 'srv-1'
    assert entry['user_id'] == 'testadmin'
    logged = json.loads(entry['changes'])['deleted']
    assert logged['created_at'] == '2026-01-01 00:00:00'
    text = entry['changes']
    for secret in ('"pw"', '"sk"', '"ak"', '"t"'):
        assert secret not in text


def test_job_delete_with_datetime_is_audited(audited_api, monkeypatch):
    import json
    client, db, main = audited_api
    job = {'id': 'job-1', 'name': 'DSpace Files', 'restic_password': 'pw',
           'created_at': datetime(2026, 4, 16), 'pitr_enabled': False}
    monkeypatch.setattr(main, 'get_job', lambda _id: dict(job))
    monkeypatch.setattr(main, 'delete_job_from_db', lambda _id: None)
    monkeypatch.setattr(main, 'refresh_schedules', lambda: None)
    assert client.delete('/api/jobs/job-1').status_code == 200
    entry = [e for e in db.logs if e['action'] == 'DELETE'][0]
    logged = json.loads(entry['changes'])['deleted']
    assert logged['name'] == 'DSpace Files' and logged['restic_password'] == '[REDACTED]'
    assert logged['created_at'] == '2026-04-16 00:00:00'

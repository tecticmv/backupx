"""
Secrets must never travel in argv (world-readable via ps / /proc/PID/cmdline),
neither on the BackupX host (ssh, sshpass) nor on the target host (the remote
shell and its child processes). Scripts go to the remote shell over stdin.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

SSH_PASSWORD = 'ssh-Pa55-SECRET'
RESTIC_PASSWORD = 'restic-Pa55-SECRET'
ACCESS_KEY = 'AKIA-ACCESS-SECRET'
SECRET_KEY = 's3-Secret-Key-SECRET'
DB_PASSWORD = 'db-Pa55 "q\' $x\\y-SECRET'
SECRETS = [SSH_PASSWORD, RESTIC_PASSWORD, ACCESS_KEY, SECRET_KEY, DB_PASSWORD]

SERVER = {'id': 'srv1', 'name': 'srv', 'host': 'target.example', 'ssh_port': 22, 'ssh_user': 'root',
          'ssh_auth_type': 'password', 'ssh_password': SSH_PASSWORD}
TARGET = {'id': 'st1', 'name': 'primary', 'endpoint': 's3.example.com', 'bucket': 'bucket',
          'access_key': ACCESS_KEY, 'secret_key': SECRET_KEY, 'skip_ssl_verify': False, 'status': 'active'}
JOB = {'id': 'job1', 'name': 'Job 1', 'backup_prefix': 'job1', 'restic_password': RESTIC_PASSWORD,
       'server_id': 'srv1', 'directories': ['/data'], 'excludes': [], 'database_config_id': 'db1'}


def _argv_text(args):
    return '\0'.join(str(a) for a in args)


def _assert_no_secret_in_argv(args):
    text = _argv_text(args)
    for secret in SECRETS:
        assert secret not in text, f'secret leaked into argv: {args!r}'
    # The MySQL password is shell-quoted inside scripts; no "-p<anything>" either
    assert not any(str(a).startswith('-p') and len(str(a)) > 2 for a in args)


class FakeTransport:
    """Records every subprocess.run / Popen call made by app.main."""

    def __init__(self, stdout_lines=None, returncode=0):
        self.calls = []
        self.stdout_lines = stdout_lines or []
        self.returncode = returncode

    def run(self, args, input=None, env=None, **kw):
        self.calls.append({'args': args, 'stdin': input or '', 'env': env})
        return subprocess.CompletedProcess(args, self.returncode, stdout='', stderr='')

    def popen(self, args, env=None, **kw):
        transport = self

        class _Stdin:
            def __init__(self):
                self.data = []

            def write(self, text):
                self.data.append(text)

            def close(self):
                pass

        class _Proc:
            def __init__(self):
                self.stdin = _Stdin()
                self.stdout = iter(line + '\n' for line in transport.stdout_lines)
                self.stderr = None
                self.returncode = transport.returncode
                transport.calls.append({'args': args, 'proc': self, 'env': env})

            def wait(self, timeout=None):
                import time
                # let the feeder thread finish writing the script
                for _ in range(100):
                    if self.stdin.data:
                        break
                    time.sleep(0.01)
                return self.returncode

            def kill(self):
                pass

        return _Proc()

    def stdin_of(self, call):
        if 'proc' in call:
            return ''.join(call['proc'].stdin.data)
        return call['stdin']


@pytest.fixture
def transport(monkeypatch):
    import app.main as main
    fake = FakeTransport()
    monkeypatch.setattr(main.subprocess, 'run', fake.run)
    monkeypatch.setattr(main.subprocess, 'Popen', fake.popen)
    monkeypatch.setattr(main, 'update_job_progress', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'update_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'add_history', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'send_notification', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'invalidate_snapshot_cache', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'get_server', lambda sid: dict(SERVER))
    return fake


def test_sshpass_reads_password_from_env():
    from app.main import _build_ssh_cmd
    cmd = _build_ssh_cmd('h', 'u', 22, ssh_auth_type='password', ssh_password=SSH_PASSWORD)
    _assert_no_secret_in_argv(cmd)
    assert cmd[:2] == ['sshpass', '-e']
    assert cmd.env()['SSHPASS'] == SSH_PASSWORD


def test_key_auth_adds_no_sshpass_env(monkeypatch):
    from app.main import _build_ssh_cmd
    monkeypatch.delenv('SSHPASS', raising=False)
    cmd = _build_ssh_cmd('h', 'u', 22, ssh_key='/k')
    assert 'SSHPASS' not in cmd.env()
    assert cmd[0] == 'ssh'


def test_filesystem_backup_sends_secrets_over_stdin_only(transport):
    import app.main as main
    main.run_filesystem_backup('job1', dict(JOB), [dict(TARGET)])

    assert len(transport.calls) == 1
    call = transport.calls[0]
    _assert_no_secret_in_argv(call['args'])
    assert call['args'][-1] == main.REMOTE_SHELL
    script = transport.stdin_of(call)
    assert RESTIC_PASSWORD in script and SECRET_KEY in script
    assert call['env']['SSHPASS'] == SSH_PASSWORD


@pytest.mark.parametrize('db_type', ['mysql', 'postgres'])
def test_database_backup_keeps_db_password_out_of_argv(transport, monkeypatch, db_type):
    import app.main as main
    monkeypatch.setattr(main, 'load_db_configs', lambda: [{
        'id': 'db1', 'type': db_type, 'host': 'db.local', 'port': 3306 if db_type == 'mysql' else 5432,
        'username': 'backup', 'password': DB_PASSWORD, 'databases': 'app', 'docker_container': ''}])
    monkeypatch.setattr(main, 'ensure_db_client_installed', lambda server, client: (True, 'ok'))

    main.run_database_backup('job1', dict(JOB), [dict(TARGET)])

    assert len(transport.calls) == 1
    call = transport.calls[0]
    _assert_no_secret_in_argv(call['args'])
    script = transport.stdin_of(call)
    if db_type == 'mysql':
        assert '--defaults-extra-file="$MYCNF"' in script
        assert ' -p' not in script.split('mysqldump', 1)[1].split('\n', 1)[0]
    else:
        assert 'PGPASSWORD=' in script


def test_ensure_restic_installed_uses_stdin(transport):
    import app.main as main
    main._restic_confirmed.clear()
    main.ensure_restic_installed('h', 'u', 22, ssh_auth_type='password', ssh_password=SSH_PASSWORD)
    call = transport.calls[0]
    _assert_no_secret_in_argv(call['args'])
    assert 'restic' in transport.stdin_of(call)


def test_db_client_install_uses_stdin(transport):
    import app.main as main
    main._db_client_confirmed.clear()
    main.ensure_db_client_installed(dict(SERVER), 'mysql')
    _assert_no_secret_in_argv(transport.calls[0]['args'])


def test_no_ssh_call_site_passes_a_script_in_argv():
    """Every ssh call goes through _ssh_run/_ssh_popen (script on stdin)."""
    import inspect
    import app.main as main
    source = inspect.getsource(main)
    assert 'ssh_cmd + [' not in source
    assert 'ssh_cmd.append(' not in source
    assert "'sshpass', '-p'" not in source
    assert '-p{' not in source  # mysql -p<password>


# --- The remote side, run for real with a local shell -----------------------

needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')


@needs_bash
def test_remote_script_survives_commands_that_read_stdin():
    from app.main import _remote_script, REMOTE_SHELL
    script = 'cat\necho after-cat\nread x || echo read-got-eof\necho done'
    result = subprocess.run(['sh', '-c', REMOTE_SHELL], input=_remote_script(script),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert result.stdout.split() == ['after-cat', 'read-got-eof', 'done']


@needs_bash
def test_remote_script_exit_code_is_kept():
    from app.main import _remote_script, REMOTE_SHELL
    result = subprocess.run(['sh', '-c', REMOTE_SHELL], input=_remote_script('echo hi\nexit 3\necho never'),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 3
    assert result.stdout.strip() == 'hi'


@needs_bash
def test_mysql_option_file_round_trips_tricky_password():
    from app.main import _mysql_option_file
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, 'my.cnf')
        script = _mysql_option_file('"$F"', DB_PASSWORD)
        assert DB_PASSWORD not in script or "'" in script  # only ever inside a quoted printf argument
        result = subprocess.run(['bash', '-c', script], env={**os.environ, 'F': path},
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        content = open(path).read().splitlines()
        assert content[0] == '[client]'
        # MySQL option-file quoting: backslash and double quote are escaped
        expected = DB_PASSWORD.replace('\\', '\\\\').replace('"', '\\"')
        assert content[1] == f'password="{expected}"'
        if os.name == 'posix':
            assert oct(os.stat(path).st_mode & 0o777) == '0o600'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- Flask routes that run remote scripts ------------------------------------

@pytest.fixture
def logged_in(client, monkeypatch):
    """An authenticated client whose login doesn't count against the login rate limit"""
    import app.main as main
    monkeypatch.setattr(main.limiter, 'enabled', False)
    response = client.post('/api/auth/login', json={'username': 'testadmin', 'password': 'testpassword123'})
    assert response.status_code == 200
    return client


def test_db_connection_test_keeps_password_out_of_argv(logged_in, transport, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'ensure_db_client_installed', lambda server, client: (True, 'ok'))
    for db_type in ('mysql', 'postgres'):
        transport.calls.clear()
        logged_in.post('/api/databases/test', json={
            'type': db_type, 'host': 'db.local', 'username': 'u', 'password': DB_PASSWORD, 'server_id': 'srv1'})
        assert len(transport.calls) == 1
        _assert_no_secret_in_argv(transport.calls[0]['args'])
        script = transport.stdin_of(transport.calls[0])
        if db_type == 'mysql':
            assert '--defaults-extra-file' in script


def test_restore_routes_keep_secrets_out_of_argv(logged_in, transport, monkeypatch):
    import app.main as main
    job = dict(JOB, backup_type='database')
    monkeypatch.setattr(main, 'get_job', lambda jid: dict(job))
    monkeypatch.setattr(main, '_job_storage_from_request', lambda j: dict(TARGET))
    monkeypatch.setattr(main, 'ensure_db_client_installed', lambda server, client: (True, 'ok'))
    monkeypatch.setattr(main, 'get_db_config', lambda cid: {
        'id': 'db1', 'type': 'mysql', 'host': 'db.local', 'port': 3306, 'username': 'u',
        'password': DB_PASSWORD, 'docker_container': ''})

    logged_in.post('/api/jobs/job1/snapshots/abc123/restore-db', json={'target_database': 'app'})
    logged_in.post('/api/jobs/job1/snapshots/abc123/restore',
                              json={'source_path': '/data', 'target_path': '/restore'})

    assert len(transport.calls) == 2
    for call in transport.calls:
        _assert_no_secret_in_argv(call['args'])
        assert RESTIC_PASSWORD in transport.stdin_of(call)
    assert '--defaults-extra-file="$RESTORE_DIR/.backupx-my.cnf"' in transport.stdin_of(transport.calls[0])

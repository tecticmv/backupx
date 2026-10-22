"""
Tests for backing up one job to several S3 storages
"""
import shutil
import subprocess

import pytest


def _target(name, endpoint, **extra):
    return {'id': name, 'name': name, 'endpoint': endpoint, 'bucket': 'bucket',
            'access_key': 'ak', 'secret_key': 'sk', 'skip_ssl_verify': False,
            'status': 'active', **extra}


JOB = {'id': 'job1', 'name': 'Job 1', 'backup_prefix': 'job1', 'restic_password': 'pw'}


@pytest.fixture
def recorded(monkeypatch):
    """Capture what a backup run records instead of touching the database"""
    import app.main as main
    calls = {'progress': [], 'status': [], 'history': []}
    monkeypatch.setattr(main, 'update_job_progress', lambda job_id, p, m: calls['progress'].append((p, m)))
    monkeypatch.setattr(main, 'update_job_status', lambda job_id, s, **kw: calls['status'].append(s))
    monkeypatch.setattr(main, 'add_history', lambda job_id, name, s, m, d=0: calls['history'].append((s, m)))
    monkeypatch.setattr(main, 'send_notification', lambda *a, **kw: None)
    return calls


def test_target_blocks_use_each_storage(recorded):
    from app.main import _restic_target_blocks

    script = _restic_target_blocks(JOB, [
        _target('primary', 's3.example.com'),
        _target('backup', 'http://minio.local:9000', skip_ssl_verify=True),
    ], 'restic backup --json /data')

    assert "RESTIC_REPOSITORY=s3:https://s3.example.com/bucket/job1" in script
    assert "RESTIC_REPOSITORY=s3:http://minio.local:9000/bucket/job1" in script
    assert script.count('restic backup --json /data') == 2
    assert 'restic backup --json /data --insecure-tls 2>&1' in script


@pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')
def test_partial_failure_is_reported_per_storage(recorded):
    """One storage failing must not stop the others, and the result names both"""
    import app.main as main

    # Fake restic as a shell function: succeeds unless the repository is the "broken" storage
    fake_restic = (
        'restic() {\n'
        '  case "$RESTIC_REPOSITORY" in *broken*) echo "Fatal: unable to open repository" >&2; return 1;; esac\n'
        '  if [ "$1" = backup ]; then\n'
        '    echo \'{"message_type":"status","percent_done":0.5,"total_bytes":10,"bytes_done":5}\'\n'
        '    echo \'{"message_type":"summary","total_bytes_processed":10,"data_added":5,'
        '"files_new":1,"files_changed":0,"total_duration":0.1}\'\n'
        '  fi\n'
        '}\n'
    )

    targets = [_target('good', 's3.example.com'), _target('broken', 'broken.example.com')]
    script = (fake_restic
              + main._restic_target_blocks(JOB, targets, 'restic backup --json /data')
              + 'exit $BACKUPX_FAILED\n')
    proc = subprocess.Popen([shutil.which('bash'), '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    returncode, stderr_text, preamble, results = main._stream_restic_progress(
        proc, 'job1', 30, [t['name'] for t in targets])

    assert returncode == 1
    assert results[0]['exit_code'] == 0
    assert results[1]['exit_code'] == 1
    assert any('unable to open repository' in line for line in results[1]['output'])

    ok, message = main._finish_backup('job1', JOB, targets, main.utc_now(), returncode, stderr_text,
                                      preamble, results, 'Backup')
    assert not ok
    assert recorded['status'] == ['failed']
    assert 'Stored on good' in message
    assert 'broken: Fatal: unable to open repository' in message


def test_all_storages_succeed(recorded):
    import app.main as main

    targets = [_target('a', 'a.example.com'), _target('b', 'b.example.com')]
    results = [{'exit_code': 0, 'output': []}, {'exit_code': 0, 'output': []}]
    ok, message = main._finish_backup('job1', JOB, targets, main.utc_now(), 0, '', [], results, 'Backup')

    assert ok
    assert recorded['status'] == ['success']
    assert 'all 2 storages' in message


def test_failure_before_any_storage_reports_dump_error(recorded):
    import app.main as main

    targets = [_target('a', 'a.example.com')]
    results = [{'exit_code': None, 'output': []}]
    ok, message = main._finish_backup('job1', JOB, targets, main.utc_now(), 1,
                                      'pg_dumpall: connection refused\n', ['pg_dumpall failed'], results,
                                      'Database backup')
    assert not ok
    assert 'connection refused' in message
    assert 'pg_dumpall failed' in message


def test_resolve_job_s3_configs(monkeypatch):
    import app.main as main

    configs = {'a': {'id': 'a'}, 'b': {'id': 'b'}}
    monkeypatch.setattr(main, 'get_s3_config', lambda cid: configs.get(cid))

    assert main._resolve_job_s3_configs({'s3_config_ids': ['a', 'b', 'a']})[0] == ['a', 'b']
    assert main._resolve_job_s3_configs({'s3_config_id': 'b'})[0] == ['b']
    assert main._resolve_job_s3_configs({'s3_config_ids': ['zzz']})[2]
    assert main._resolve_job_s3_configs({'s3_config_ids': 'a'})[2]

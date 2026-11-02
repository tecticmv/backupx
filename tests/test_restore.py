"""
Restore wizard (P2): pure decisions, plans, gating of replace-production,
task cleanup, pre-restore retention safety and secrets.

Docker/restic/SSH are faked here; the end-to-end rig (see docs/UX-OVERHAUL.md)
runs the real thing against throwaway containers.
"""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app import restore as r
from app import restore_scripts as rs
from app import retention, tasks

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


# =============================================================================
# Pure
# =============================================================================

def test_disk_check_uses_factor_and_explains():
    disk = r.disk_check('/data', 9 * 1024 ** 3, 4 * 1024 ** 3)
    assert disk['needed_bytes'] == 10 * 1024 ** 3 and not disk['ok']
    assert 'x 2.5' in r.disk_error(disk, 4 * 1024 ** 3)
    assert r.disk_check('/data', 11 * 1024 ** 3, 4 * 1024 ** 3)['ok']


def test_estimate_restored_bytes():
    assert r.estimate_restored_bytes('dump', 100, 0) == 500
    assert r.estimate_restored_bytes('binlog', 100, 50) == 550
    assert r.estimate_restored_bytes('walg', None, 10, 1000) == 1010
    assert r.estimate_restored_bytes('files', 77) == 77


def test_gaps_and_windows():
    w = [{'from': NOW - timedelta(days=2), 'to': NOW - timedelta(days=1), 'anchored': True, 'reason': 'start'},
         {'from': NOW - timedelta(hours=20), 'to': NOW, 'anchored': True, 'reason': 'gap event'}]
    gaps = r.pitr_gaps(w)
    assert len(gaps) == 1 and gaps[0]['reason'] == 'gap event'
    assert r.time_in_windows(NOW - timedelta(hours=1), w)
    assert not r.time_in_windows(NOW - timedelta(hours=22), w)


def test_latest_point_prefers_pitr_then_newest_non_pre_restore():
    w = [{'from': NOW - timedelta(days=1), 'to': NOW.replace(microsecond=500), 'anchored': True}]
    assert r.latest_point('mysql', [], w, True)['point_type'] == 'time'
    snaps = [{'id': 'a' * 64, 'time': '2026-11-01T10:00:00Z', 'tags': ['automated']},
             {'id': 'b' * 64, 'time': '2026-11-01T11:00:00Z', 'tags': ['pre-restore']}]
    latest = r.latest_point('mysql', snaps, [], False)
    assert latest['point_type'] == 'snapshot' and latest['snapshot_id'] == 'a' * 64
    assert r.latest_point('files', [], w, True) is None


def test_server_version_and_images():
    assert r.server_version_from_dump('-- MySQL dump 10.13\n-- Server version\t8.4.3\n') == '8.4.3'
    v = r.server_version_from_dump('-- MariaDB dump 10.19\n-- Server version\t10.6.18-MariaDB-0ubuntu0\n')
    assert r.scratch_image('mariadb', v) == 'mariadb:10.6'
    assert r.scratch_image('mysql', '8.4.3') == 'mysql:8.4'
    pg = r.server_version_from_dump('--\n-- Dumped from database version 10.23 (Ubuntu 10.23-1)\n')
    assert r.scratch_image('postgres', pg) == 'postgres:10'
    assert r.scratch_image('postgres', 'PostgreSQL 9.6.24') == 'postgres:9.6'
    assert r.walg_scratch_image('10') == 'postgres:10-bullseye'


def test_compare_judges_key_tables_on_exact_counts_only():
    rows = r.compare({'a.t': 10, 'a.u': 5}, {'a.t': 9, 'a.u': 5}, ['a.t'], {'a.t': 10}, {'a.t': 10})
    assert rows[0]['name'] == 'a.t' and rows[0]['key'] and rows[0]['match'] is True
    assert next(x for x in rows if x['name'] == 'a.u')['match'] is None
    rows = r.compare({}, {}, ['a.t'], {'a.t': 10}, {'a.t': 11})
    assert r.key_mismatches(rows) == ['a.t']


def test_verify_config_validation():
    cfg, errors = r.normalize_verify_config({'key_tables': 'app.users, app.orders\nbad name', 'check_sql': 'DROP x',
                                             'health_url': 'ftp://x'}, 'mysql')
    assert cfg['key_tables'] == ['app.users', 'app.orders']
    assert len(errors) == 4  # 'bad name' splits into two invalid words
    cfg, errors = r.normalize_verify_config({'key_tables': ['dspace.item'], 'check_sql': 'SELECT 1;'}, 'postgres')
    assert cfg['key_tables'] == ['dspace.public.item'] and cfg['check_sql'] == 'SELECT 1' and not errors
    _cfg, errors = r.normalize_verify_config({'check_sql': 'SELECT 1; DELETE FROM t'}, 'mysql')
    assert errors


def test_parse_marked_output():
    out = r.parse_marked_output('noise\nBXSQL-BEGIN est\napp\tt\t3\nBXSQL-END est 0\nBXSQL-BEGIN exact:app.t\n'
                                'ERROR 1146\nBXSQL-END exact:app.t 1\n')
    assert out['est'] == (0, ['app\tt\t3']) and out['exact:app.t'][0] == 1
    assert r.parse_counts(out['est'][1]) == {'app.t': 3}


def test_restore_paths_must_be_inside_backed_up_dirs():
    assert r.validate_restore_paths(['/srv/a', '/srv/a/'], ['/srv'])[0] == ['/srv/a']
    assert r.validate_restore_paths(['/etc/passwd'], ['/srv'])[1]
    assert r.validate_restore_paths(['/srv/../etc'], ['/srv'])[1]
    assert r.validate_restore_paths(['/'], [])[1]
    assert r.validate_restore_paths([], ['/srv'])[1]


def test_extend_is_capped():
    until = r.extend_until(NOW + timedelta(hours=5), now=NOW, hours=24)
    assert until == NOW + timedelta(hours=29)
    assert r.extend_until(NOW + timedelta(days=6, hours=20), now=NOW, hours=24) == NOW + timedelta(days=7)


def test_confirm_text_is_database_name():
    assert r.confirm_text({'name': 'E-Petition DB', 'databases': 'epetition'}) == 'epetition'
    assert r.confirm_text({'name': 'Koha DB', 'databases': '*'}) == 'Koha DB'


def test_scratch_container_args_have_label_and_no_ports():
    args = rs.docker_args('abc123', 'job1')
    assert "backupx.restore=abc123" in args and '--network none' in args
    assert '-p ' not in rs.mysql_start('mysql') and '--publish' not in rs.mysql_start('mysql')


def test_scratch_dump_flags():
    s = rs.scratch_dump('mysql', 'c', ['app'], '/o.sql.gz', strip_definers=True)
    assert '--set-gtid-purged=OFF' in s and '--add-drop-database' in s and 'DEFINER' in s
    assert '--routines' in s and '--triggers' in s and '--events' in s
    s = rs.scratch_dump('mariadb', 'c', ['app'], '/o.sql.gz')
    assert 'set-gtid-purged' not in s and 'mariadb-dump' in s
    assert '--clean --if-exists --create' in rs.scratch_dump('postgres', 'c', ['d'], '/o')


def test_prod_runner_keeps_password_out_of_argv_positions():
    cfg = {'host': 'db.example', 'port': 3306, 'username': 'app', 'password': 'Db-Secret-pw1', 'databases': '*'}
    s = rs.prod_runner(cfg, 'mysql')
    # The password only appears inside the option file written with printf (a builtin)
    assert s.count('Db-Secret-pw1') == 1
    lines = s.splitlines()
    i = next(n for n, l in enumerate(lines) if 'Db-Secret-pw1' in l)
    # inside the quoted option-file text that printf (a builtin) writes to a 0600 file
    assert "printf '%s'" in ' '.join(lines[max(0, i - 3):i + 1]) and lines[i].startswith('password=')
    assert '--defaults-extra-file' in s
    s = rs.prod_runner(dict(cfg, docker_container='koha-db'), 'mariadb')
    assert 'docker exec -e' not in s and "cat > /tmp/.backupx-restore.cnf" in s
    s = rs.prod_runner(dict(cfg, port=5432), 'postgres')
    assert 'export PGPASSWORD=' in s
    s = rs.prod_runner(cfg, 'mysql', client_image='percona/percona-server:8.4')
    assert '__BACKUPX_CNF_END__' in s and ' -v "' not in s and ' -v /' not in s  # no volume mounts


def test_files_restore_script_takes_pre_restore_backup_first():
    job = {'restic_password': 'Restic-pw-1'}
    target = {'access_key': 'AK', 'secret_key': 'SK-secret-1'}
    s = rs.files_restore_script(job, target, 's3:x', 'snap1', ['/srv/a'], '/', True, 'rid1')
    assert s.index('--tag pre-restore') < s.index('restic restore')
    assert 'BACKUPX-PRE-RESTORE-SNAPSHOT' in s
    assert 'pre-restore' not in rs.files_restore_script(job, target, 's3:x', 'snap1', ['/srv/a'], '/x', False, 'r')


# =============================================================================
# Pre-restore snapshots and retention
# =============================================================================

def test_pre_restore_runs_are_not_tagged_automated(monkeypatch):
    import app.main as main
    run = main.BackupRun('j', tags=['pre-restore', 'restore:abc'])
    with main._backup_run_context(run):
        args = main._run_tag_args()
    assert 'automated' not in args and '--tag pre-restore' in args and "--tag restore:abc" in args
    with main._backup_run_context(main.BackupRun('j')):
        assert main._run_tag_args() == '--tag automated'


def test_retention_forget_only_looks_at_automated_snapshots():
    args = retention.forget_plan_args({'retention_daily': 1, 'retention_hourly': 0, 'retention_weekly': 0,
                                       'retention_monthly': 0, 'backup_type': 'database'})
    i = args.index('--tag')
    assert args[i + 1] == 'automated'


# =============================================================================
# Service (test Postgres)
# =============================================================================

@pytest.fixture
def db():
    import app.main  # noqa: F401
    from app.db.factory import get_database
    database = get_database()

    def clean():
        database.rollback()
        for sql in ("DELETE FROM restores WHERE job_id LIKE 'rx%%'", "DELETE FROM task_logs", "DELETE FROM tasks",
                    "DELETE FROM jobs WHERE id LIKE 'rx%%'", "DELETE FROM servers WHERE id LIKE 'rx%%'",
                    "DELETE FROM db_configs WHERE id LIKE 'rx%%'", "DELETE FROM s3_configs WHERE id LIKE 'rx%%'"):
            database.execute(sql)
        database.commit()
    clean()
    yield database
    clean()


SNAPS = [{'id': 'f' * 64, 'short_id': 'ffffffff', 'time': '2026-10-31T21:00:00Z',
          'paths': ['/tmp/tmp.x/mysql_backup_1.sql.gz'], 'tags': ['automated', 'mysql-backup'],
          'summary': {'total_bytes_processed': 1000}}]


@pytest.fixture
def world(db, monkeypatch, tmp_path):
    import app.main as main
    main.create_server({'id': 'rxsrv', 'name': 'rx-db', 'host': 'rx-db.lan', 'ssh_port': 22, 'ssh_user': 'root',
                        'ssh_auth_type': 'password', 'ssh_password': 'Ssh-Secret-pw1'})
    main.create_s3_config({'id': 'rxs1', 'name': 'RX Primary', 'endpoint': 'rxs1.example.com', 'bucket': 'bk',
                           'access_key': 'AK-rxs1', 'secret_key': 'SK-rxs1-secret'})
    main.create_db_config({'id': 'rxdb', 'name': 'RX DB', 'type': 'mysql', 'host': '127.0.0.1', 'port': 3306,
                           'username': 'app', 'password': 'Db-Secret-pw1', 'databases': 'app'})
    main.save_job('rxjob', {'name': 'RX DB job', 'backup_type': 'database', 'server_id': 'rxsrv',
                            'database_config_id': 'rxdb', 's3_config_id': 'rxs1', 's3_config_ids': ['rxs1'],
                            'restic_password': 'Restic-Secret-pw1', 'backup_prefix': 'rxjob'})
    main.save_job('rxfiles', {'name': 'RX files', 'backup_type': 'filesystem', 'server_id': 'rxsrv',
                              's3_config_id': 'rxs1', 's3_config_ids': ['rxs1'], 'directories': ['/srv'],
                              'restic_password': 'Restic-Secret-pw1', 'backup_prefix': 'rxfiles'})
    monkeypatch.setattr(main, 'get_snapshots', lambda job, target: [dict(s) for s in SNAPS])
    monkeypatch.setattr(main.limiter, 'enabled', False)
    monkeypatch.setenv('RESTORE_WORK_DIR', str(tmp_path / 'restores'))
    return main


@pytest.fixture
def api(client, world):
    assert client.post('/api/auth/login', json={'username': 'testadmin', 'password': 'testpassword123'}).status_code == 200
    return client


@pytest.fixture
def runner(db):
    import app.main as main
    rr = tasks.TaskRunner(main._lock_connection, max_workers=1, worker_id='test-restore')
    yield rr
    rr.stop(wait=True)


def _plan(api, **body):
    data = dict({'job_id': 'rxjob', 'point_type': 'snapshot', 'snapshot_id': 'f' * 64, 'target': 'scratch'}, **body)
    res = api.post('/api/restores/plan', json=data)
    return data, res


def test_plan_and_start_need_matching_hash(api, monkeypatch):
    from app import restore_service as svc
    monkeypatch.setattr(svc, '_free_bytes', lambda path: 10 ** 12)
    data, res = _plan(api)
    assert res.status_code == 200
    body = res.get_json()
    assert body['ok'] and body['plan']['method'] == 'dump' and body['plan']['dump']['size'] == 1000
    assert body['plan']['estimated_restored_bytes'] == 5000
    assert 'dump of' in body['plan']['summary']
    r1 = api.post('/api/restores', json=data)
    assert r1.status_code == 400 and r1.get_json()['code'] == 'plan_hash_missing'
    r2 = api.post('/api/restores', json=dict(data, plan_hash='0' * 64))
    assert r2.status_code == 409 and r2.get_json()['code'] == 'plan_changed' and r2.get_json()['plan_hash']
    r3 = api.post('/api/restores', json=dict(data, plan_hash=body['plan_hash']))
    assert r3.status_code == 202
    out = r3.get_json()
    assert out['restore']['status'] == 'running' and out['task']['kind'] == 'restore_scratch'
    assert out['restore']['container'] == 'backupx-restore-' + out['restore']['id']
    # Secrets never in task params
    assert 'Secret' not in json.dumps(out['task']['params'])
    # a second scratch restore of the same job while one is queued: busy
    r4 = api.post('/api/restores', json=dict(data, plan_hash=body['plan_hash']))
    assert r4.status_code == 409 and r4.get_json()['code'] == 'busy'


def test_disk_precheck_refuses(api, monkeypatch):
    from app import restore_service as svc
    monkeypatch.setattr(svc, '_free_bytes', lambda path: 100)
    data, res = _plan(api)
    body = res.get_json()
    assert not body['ok'] and 'Not enough free disk' in body['error']
    r2 = api.post('/api/restores', json=dict(data, plan_hash=body['plan_hash']))
    assert r2.status_code == 400 and r2.get_json()['code'] == 'insufficient_disk'


def test_exact_time_needs_pitr_and_download_of_time_uses_script(api):
    _data, res = _plan(api, point_type='time', target_time='2026-10-31T22:00:00Z')
    assert res.status_code == 400 and res.get_json()['code'] == 'not_restorable'


def test_restore_points(api):
    res = api.get('/api/jobs/rxjob/restore-points')
    body = res.get_json()
    assert res.status_code == 200 and body['engine'] == 'mysql' and body['latest']['point_type'] == 'snapshot'
    assert body['pitr']['enabled'] is False and body['snapshots']


def test_verify_config_roundtrip(api):
    res = api.put('/api/jobs/rxjob/restore-verify', json={'key_tables': ['app.users'], 'check_sql': 'SELECT 1'})
    assert res.status_code == 200 and res.get_json()['key_tables'] == ['app.users']
    assert api.get('/api/jobs/rxjob/restore-verify').get_json()['check_sql'] == 'SELECT 1'
    assert api.put('/api/jobs/rxjob/restore-verify', json={'key_tables': ['x y']}).status_code == 400


def _ready_restore(status='ready', **extra):
    from app import restore_service as svc
    rid = tasks.new_id()
    row = dict({'id': rid, 'job_id': 'rxjob', 'job_name': 'RX DB job', 'engine': 'mysql', 'point_type': 'snapshot',
                'snapshot_id': 'f' * 64, 'storage_id': 'rxs1', 'target_type': 'scratch', 'server': 'backupx',
                'container': r.container_name(rid), 'plan': {'method': 'dump', 'estimated_restored_bytes': 5000},
                'info': {'databases': ['app']}, 'status': status,
                'cleanup_at': (datetime.now(timezone.utc) + timedelta(hours=20)).replace(tzinfo=None)}, **extra)
    return svc.create_row(row)


def test_replace_refused_without_scratch_typed_text_or_step_up(api, monkeypatch):
    running = _ready_restore(status='running')
    res = api.post(f"/api/restores/{running['id']}/replace-production", json={'preview': True})
    assert res.status_code == 409 and res.get_json()['code'] == 'not_ready'
    ready = _ready_restore()
    url = f"/api/restores/{ready['id']}/replace-production"
    prev = api.post(url, json={'preview': True}).get_json()
    assert prev['confirm_text'] == 'app' and prev['plan']['production']['databases'] == ['app']
    assert any('pre-restore' in s for s in prev['plan']['steps'])
    # No step-up
    res = api.post(url, json={'plan_hash': prev['plan_hash'], 'confirm': 'app'})
    assert res.status_code == 403 and res.get_json()['code'] == 'step_up_required'
    assert api.post('/api/auth/step-up', json={'password': 'testpassword123'}).status_code == 200
    res = api.post(url, json={'plan_hash': prev['plan_hash'], 'confirm': 'wrong'})
    assert res.status_code == 400 and res.get_json()['code'] == 'confirmation_mismatch'
    res = api.post(url, json={'confirm': 'app'})
    assert res.status_code == 400 and res.get_json()['code'] == 'plan_hash_missing'
    res = api.post(url, json={'plan_hash': 'x' * 64, 'confirm': 'app'})
    assert res.status_code == 409 and res.get_json()['code'] == 'plan_changed'
    res = api.post(url, json={'plan_hash': prev['plan_hash'], 'confirm': 'app'})
    assert res.status_code == 202
    task = res.get_json()['task']
    assert task['kind'] == 'restore_replace' and task['tier'] == 'maintenance' and not task['cancellable']
    # Undo isn't possible before a replace happened
    res = api.post(f"/api/restores/{ready['id']}/undo", json={'preview': True})
    assert res.status_code == 409


def test_replace_task_refuses_changed_plan(world, runner, monkeypatch):
    from app import restore_service as svc
    ready = _ready_restore()
    tasks.enqueue('restore_replace', {'restore_id': ready['id'], 'job_id': 'rxjob', 'plan_hash': 'stale'})
    called = []
    monkeypatch.setattr(world, 'run_backup', lambda *a, **k: called.append(1) or (True, 'ok'))
    assert runner.run_once() == 'failed'
    assert not called and svc.get_row(ready['id'])['status'] == 'ready'


def test_replace_flow_pre_restore_must_succeed(world, runner, monkeypatch):
    from app import restore_service as svc
    ready = _ready_restore()
    plan, _ = svc.replace_plan(ready)
    from app.security import plan_hash
    tasks.enqueue('restore_replace', {'restore_id': ready['id'], 'job_id': 'rxjob', 'plan_hash': plan_hash(plan)})
    monkeypatch.setattr(world, 'run_backup', lambda *a, **k: (False, 'storage down'))
    touched = []
    monkeypatch.setattr(svc, 'run_local', lambda *a, **k: touched.append('local') or 0)
    assert runner.run_once() == 'failed'
    row = svc.get_row(ready['id'])
    assert row['status'] == 'ready' and 'pre-restore backup failed' in row['error'] and not touched


def test_replace_flow_records_pre_restore_and_rolls_back_on_key_mismatch(world, runner, monkeypatch):
    from app import restore_service as svc
    from app.security import plan_hash
    ready = _ready_restore()
    plan, _ = svc.replace_plan(ready)
    task_row, _ = tasks.enqueue('restore_replace', {'restore_id': ready['id'], 'job_id': 'rxjob',
                                                    'plan_hash': plan_hash(plan)})
    seen_tags = []

    def fake_backup(job_id, trigger=None, task=None, tags=None, **k):
        seen_tags.append(tags)
        task.result = {'storages': [{'id': 'rxs1', 'name': 'RX Primary', 'snapshot_id': 'p' * 64}]}
        return True, 'Database backup completed'
    monkeypatch.setattr(world, 'run_backup', fake_backup)
    monkeypatch.setattr(svc, 'run_local', lambda ctx, script, **k: 0)
    monkeypatch.setattr(svc, '_upload', lambda server, path, timeout: '/tmp/backupx-restore.abc123')

    class P:
        pass
    ssh_scripts = []

    def fake_popen(cmd, script):
        ssh_scripts.append(script)
        return subprocess.Popen(['sh', '-c', 'echo BACKUPX-IMPORT-OK'], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    monkeypatch.setattr(world, '_ssh_popen', fake_popen)
    runs = []

    def fake_run(cmd, script, timeout):
        runs.append(script)
        return subprocess.CompletedProcess([], 0, 'BACKUPX-ROLLBACK-OK\n', '')
    monkeypatch.setattr(world, '_ssh_run', fake_run)
    monkeypatch.setattr(svc, 'run_verify', lambda row, phase='scratch': {
        'tables': [{'name': 'app.users', 'key': True, 'match': False}], 'prod_error': None,
        'summary': {'tables': 1, 'key_tables': 1, 'key_mismatches': 1}})
    assert runner.run_once() == 'rolled_back'
    assert seen_tags == [['pre-restore', f"restore:{ready['id']}"]]
    row = svc.get_row(ready['id'])
    assert json.loads(row['pre_restore_snapshot'])[0]['snapshot_id'] == 'p' * 64
    assert row['status'] == 'ready'
    assert any('rollback.sql.gz' in s for s in runs)          # rollback copy imported
    assert any('backupx-restore.abc123' in s and 'rm -rf' in s for s in runs)   # remote dir cleaned
    # secrets: the DB password only went over stdin; never in task logs/params
    logs = ' '.join(l['line'] for l in tasks.get_logs(task_row['id'], limit=5000))
    for secret in ('Db-Secret-pw1', 'Restic-Secret-pw1', 'SK-rxs1-secret', 'Ssh-Secret-pw1'):
        assert secret not in logs and secret not in json.dumps(tasks.get_task(task_row['id'])['params'])


def test_replace_flow_success_then_undo_allowed(world, runner, monkeypatch):
    from app import restore_service as svc
    from app.security import plan_hash
    ready = _ready_restore()
    plan, _ = svc.replace_plan(ready)
    tasks.enqueue('restore_replace', {'restore_id': ready['id'], 'job_id': 'rxjob', 'plan_hash': plan_hash(plan)})

    def fake_backup(job_id, trigger=None, task=None, tags=None, **k):
        task.result = {'storages': [{'id': 'rxs1', 'name': 'RX Primary', 'snapshot_id': 'p' * 64}]}
        return True, 'ok'
    monkeypatch.setattr(world, 'run_backup', fake_backup)
    monkeypatch.setattr(svc, 'run_local', lambda ctx, script, **k: 0)
    monkeypatch.setattr(svc, '_upload', lambda server, path, timeout: '/tmp/backupx-restore.abc123')
    monkeypatch.setattr(world, '_ssh_popen', lambda cmd, script: subprocess.Popen(
        ['sh', '-c', 'echo BACKUPX-IMPORT-OK'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    monkeypatch.setattr(world, '_ssh_run', lambda cmd, script, timeout: subprocess.CompletedProcess([], 0, '', ''))
    monkeypatch.setattr(svc, 'run_verify', lambda row, phase='scratch': {
        'tables': [{'name': 'app.users', 'key': True, 'match': True}], 'prod_error': None, 'health_url': None,
        'summary': {'tables': 1, 'key_tables': 1, 'key_mismatches': 0}})
    assert runner.run_once() == 'succeeded'
    row = svc.get_row(ready['id'])
    ser = svc.serialize(row)
    assert row['status'] == 'replaced' and ser['can_undo'] and ser['pre_restore_expires_at']
    plan_u, phrase = svc.replace_plan(row, 'undo')
    assert phrase == 'app' and 'pre-restore snapshot pppppppp' in plan_u['source']['point']


def _fake_local(monkeypatch, svc, rc=0, raise_exc=None):
    calls = []

    def fake_run_local(ctx, script, env=None, timeout=None, on_line=None):
        calls.append(script)
        if raise_exc:
            raise raise_exc
        return rc
    monkeypatch.setattr(svc, 'run_local', fake_run_local)
    return calls


def test_scratch_failure_and_cancel_remove_container(world, runner, monkeypatch, tmp_path):
    from app import restore_service as svc
    import gzip as gz
    monkeypatch.setattr(svc.shutil, 'which', lambda name: '/usr/bin/' + name)

    def fake_restore(ctx, job, target, snapshot_id, dest, includes=()):
        os.makedirs(dest, exist_ok=True)
        with gz.open(os.path.join(dest, 'mysql_backup_1.sql.gz'), 'wt') as f:
            f.write('-- MySQL dump 10.13\n-- Server version\t8.4.3\nSELECT 1;\n')
    monkeypatch.setattr(svc, '_restic_restore', fake_restore)
    removed = []
    monkeypatch.setattr(svc, 'remove_scratch', lambda row, log=None: removed.append(row['id']))
    calls = _fake_local(monkeypatch, svc, rc=1)
    row = _ready_restore(status='running', plan={'method': 'dump', 'dump': {'snapshot_id': 'f' * 64}, 'image': None})
    tasks.enqueue('restore_scratch', {'restore_id': row['id'], 'job_id': 'rxjob'})
    assert runner.run_once() == 'failed'
    assert removed == [row['id']] and svc.get_row(row['id'])['status'] == 'failed'
    assert 'mysql:8.4' in calls[0] and "backupx.restore=" + row['id'] in calls[0]
    # cancel mid-load
    _fake_local(monkeypatch, svc, raise_exc=tasks.TaskCancelled())
    row2 = _ready_restore(status='running', plan={'method': 'dump', 'dump': {'snapshot_id': 'f' * 64}})
    tasks.enqueue('restore_scratch', {'restore_id': row2['id'], 'job_id': 'rxjob'})
    assert runner.run_once() == 'cancelled'
    assert row2['id'] in removed and svc.get_row(row2['id'])['status'] == 'cancelled'


def test_scratch_success_sets_ttl_and_verify(world, runner, monkeypatch):
    from app import restore_service as svc
    import gzip as gz
    monkeypatch.setattr(svc.shutil, 'which', lambda name: '/usr/bin/' + name)

    def fake_restore(ctx, job, target, snapshot_id, dest, includes=()):
        os.makedirs(dest, exist_ok=True)
        with gz.open(os.path.join(dest, 'd.sql.gz'), 'wt') as f:
            f.write('-- Server version\t8.4.3\n')
    monkeypatch.setattr(svc, '_restic_restore', fake_restore)
    _fake_local(monkeypatch, svc, rc=0)
    monkeypatch.setattr(svc, 'run_verify', lambda row, phase='scratch': {'summary': {'tables': 2}, 'tables': []})
    row = _ready_restore(status='running', plan={'method': 'dump', 'dump': {'snapshot_id': 'f' * 64}})
    tasks.enqueue('restore_scratch', {'restore_id': row['id'], 'job_id': 'rxjob'})
    assert runner.run_once() == 'succeeded'
    got = svc.get_row(row['id'])
    assert got['status'] == 'ready' and got['cleanup_at'] is not None
    assert svc.serialize(got)['can_replace']
    assert 'mysql -uroot' in svc.serialize(got)['connect_hint']


def test_cleanup_interrupted_expired_and_orphans(world, monkeypatch):
    from app import restore_service as svc
    removed = []
    monkeypatch.setattr(svc, 'remove_scratch', lambda row, log=None: removed.append(row['id']))
    monkeypatch.setattr(svc.shutil, 'which', lambda name: None)
    # worker died: task failed "interrupted", row still running
    t, _ = tasks.enqueue('restore_scratch', {'restore_id': 'x'})
    db = world.get_db()
    db.execute("UPDATE tasks SET status='failed', error='interrupted' WHERE id=%s", (t['id'],))
    db.commit()
    dead = _ready_restore(status='running', task_id=t['id'])
    expired = _ready_restore(cleanup_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None))
    fresh = _ready_restore()
    summary = svc.cleanup(docker_list=[['orphan1', 'backupx-restore-orphan1', 'Up']])
    assert summary['interrupted'] == [dead['id']] and summary['expired'] == [expired['id']]
    assert summary['orphans'] == ['orphan1']
    assert svc.get_row(dead['id'])['status'] == 'failed' and svc.get_row(dead['id'])['error'] == 'interrupted'
    assert svc.get_row(expired['id'])['status'] == 'expired'
    assert svc.get_row(fresh['id'])['status'] == 'ready' and fresh['id'] not in removed


def test_pre_restore_snapshots_expire_only_after_keep_days_with_retention_on(world, monkeypatch):
    from app import restore_service as svc
    forgot = []
    monkeypatch.setattr(world, '_restic_local', lambda job, target, args, timeout, lock_wait=None:
                        forgot.append(args) or subprocess.CompletedProcess([], 0, '', ''))
    pre = [{'storage_id': 'rxs1', 'storage_name': 'RX Primary', 'snapshot_id': 'p' * 64}]
    young = _ready_restore(status='replaced', pre_restore_snapshot=pre,
                           pre_restore_at=(datetime.now(timezone.utc) - timedelta(days=29)).replace(tzinfo=None))
    old = _ready_restore(status='replaced', pre_restore_snapshot=pre,
                         pre_restore_at=(datetime.now(timezone.utc) - timedelta(days=31)).replace(tzinfo=None))
    monkeypatch.setenv('RETENTION_ENABLED', 'false')
    assert svc.expire_pre_restore() == [] and not forgot
    monkeypatch.setenv('RETENTION_ENABLED', 'true')
    assert svc.expire_pre_restore() == [old['id']]
    assert forgot == [['forget', 'p' * 64]]
    assert svc.get_row(young['id'])['pre_restore_forgotten_at'] is None


def test_file_restore_original_needs_overwrite_and_step_up(api, monkeypatch):
    monkeypatch.setattr(api.application, 'testing', True)
    import app.main as main
    monkeypatch.setattr(main, 'get_snapshots', lambda job, target: [
        {'id': 'e' * 64, 'short_id': 'eeeeeeee', 'time': '2026-10-31T21:00:00Z', 'paths': ['/srv']}])
    body = {'job_id': 'rxfiles', 'snapshot_id': 'e' * 64, 'paths': ['/srv/app/config.php'], 'target': 'original'}
    prev = api.post('/api/restores/files/plan', json=body).get_json()
    assert prev['confirm_text'] == 'overwrite' and 'Pre-restore backup' in prev['plan']['steps'][0]
    res = api.post('/api/restores/files', json=dict(body, plan_hash=prev['plan_hash'], confirm='overwrite'))
    assert res.status_code == 403
    api.post('/api/auth/step-up', json={'password': 'testpassword123'})
    res = api.post('/api/restores/files', json=dict(body, plan_hash=prev['plan_hash'], confirm='nope'))
    assert res.status_code == 400
    res = api.post('/api/restores/files', json=dict(body, plan_hash=prev['plan_hash'], confirm='overwrite'))
    assert res.status_code == 202 and res.get_json()['task']['kind'] == 'restore_files_paths'
    bad = api.post('/api/restores/files/plan', json=dict(body, paths=['/etc/shadow'])).get_json()
    assert bad['code'] == 'invalid'


def test_local_scripts_run_with_secrets_only_in_env(world, monkeypatch):
    from app import restore_service as svc
    seen = {}

    class FakeProc:
        stdin = None

    def fake_popen(args, **kw):
        seen['args'] = args
        seen['env'] = kw.get('env')
        raise OSError('stop')
    monkeypatch.setattr(svc.subprocess, 'Popen', fake_popen)
    with pytest.raises(OSError):
        svc._popen_local('echo hi', {'RESTIC_PASSWORD': 'Restic-Secret-pw1'})
    assert seen['args'] == ['bash', '-s'] and seen['env']['RESTIC_PASSWORD'] == 'Restic-Secret-pw1'


# =============================================================================
# Production comparison is opt-in; dashboard counts drills and restores as running
# =============================================================================

def test_scratch_restore_does_not_compare_with_production_by_default(api, monkeypatch):
    from app import restore_service as svc
    monkeypatch.setattr(svc, '_free_bytes', lambda path: 10 ** 12)
    data, res = _plan(api)
    plan = res.get_json()
    assert plan['plan']['compare_production'] is False
    assert not any('production' in s and 'not queried' not in s for s in plan['plan']['steps'])
    assert plan['plan']['steps'][-1] == 'Count rows in the restored copy (production not queried)'
    out = api.post('/api/restores', json=dict(data, plan_hash=plan['plan_hash'])).get_json()
    row = svc.get_row(out['restore']['id'])
    assert json.loads(row['info'])['compare_production'] is False and not svc.compares_production(row)


def test_scratch_restore_compares_with_production_when_ticked(api, monkeypatch):
    from app import restore_service as svc
    monkeypatch.setattr(svc, '_free_bytes', lambda path: 10 ** 12)
    data, res = _plan(api, compare_production=True)
    plan = res.get_json()
    assert plan['plan']['compare_production'] is True
    assert plan['plan']['steps'][-1] == 'Compare row counts with production'
    # the choice is part of the reviewed plan: starting with the other choice is a changed plan
    unticked = dict(data, compare_production=False, plan_hash=plan['plan_hash'])
    assert api.post('/api/restores', json=unticked).get_json()['code'] == 'plan_changed'
    out = api.post('/api/restores', json=dict(data, plan_hash=plan['plan_hash'])).get_json()
    assert svc.compares_production(svc.get_row(out['restore']['id']))


def test_only_an_explicit_true_turns_the_comparison_on():
    from app import restore_service as svc
    for value in (None, False, 'true', 1, 'yes'):
        assert svc.wants_production_compare({'compare_production': value}) is False
    assert svc.wants_production_compare({}) is False and svc.wants_production_compare(None) is False
    assert svc.wants_production_compare({'compare_production': True}) is True


def test_verify_line_for_copy_only_checks():
    from app import restore_service as svc
    line = svc._verify_line({'production': False, 'summary': {'tables': 7}, 'prod_error': 'not compared: x'})
    assert line == 'Counted rows in 7 tables of the restored copy (production not queried)'
    assert svc._verify_line({'production': True, 'summary': {'tables': 3}}).startswith('Compared 3 tables')


def test_dashboard_counts_running_drills_and_restores(api, db):
    from app import health_service as hs
    from app import restore_service as svc
    assert hs.job_activity().get('rxjob') is None
    # a ready scratch copy is not running anything
    _ready_restore(status='ready')
    assert hs.job_activity().get('rxjob') is None
    # a scratch restore a person started is running
    running = _ready_restore(status='running')
    assert hs.job_activity()['rxjob'] == ['restore']
    svc.update_row(running['id'], status='removed')
    # weekly drills: the drill copy counts while the drill task runs (also while it is 'ready' for checksums)
    drill_row = _ready_restore(status='ready', info={'drill': 'A', 'compare_production': False})
    assert hs.job_activity().get('rxjob') is None  # no drill task: a leftover copy, not a running drill
    task, _ = tasks.enqueue('restore_drill_all', {'scheduled': True}, title='Weekly restore drills')
    assert hs.job_activity()['rxjob'] == ['drill']
    card = next(c for c in api.get('/api/dashboard/overview').get_json()['jobs'] if c['id'] == 'rxjob')
    assert card['activity'] == ['drill'] and card['running'] is False
    svc.update_row(drill_row['id'], status='removed')
    assert hs.job_activity().get('rxjob') is None  # weekly task between jobs
    # a per-job drill task counts for its job straight away
    tasks.enqueue('restore_drill', {'job_id': 'rxfiles'}, title='Restore drill', resource_type='job',
                  resource_id='rxfiles')
    busy = hs.job_activity()
    assert busy.get('rxfiles') == ['drill'] and busy.get('rxjob') is None

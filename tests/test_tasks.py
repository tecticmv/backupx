"""
Background task system (app/tasks.py), the task API, CSRF, step-up re-auth,
confirmed plans, per-storage run results and the operations moved into tasks.

Runs against the test Postgres (like the rest of the suite); the runner is
driven by hand (TASK_RUNNER_ENABLED=false in conftest).
"""
import json
import os
import subprocess
import threading
import time
from datetime import timedelta

import pytest

from app import tasks
from app.security import (ConfirmationError, plan_hash, present_plan, verify_confirmation)


# --- Test task kinds -----------------------------------------------------------

@tasks.register('test_echo', title='Echo')
def _echo(ctx):
    ctx.add_secret(ctx.params.get('secret_value_for_test') or '')
    for line in ctx.params.get('lines', []):
        ctx.log(line)
    return {'echo': ctx.params.get('value')}


@tasks.register('test_fail', title='Fail')
def _fail(ctx):
    raise tasks.TaskError('it broke')


@tasks.register('test_wait', title='Wait for cancel')
def _wait(ctx):
    for _ in range(200):
        ctx.check_cancelled()
        time.sleep(0.05)
    return {'finished': True}


@tasks.register('test_uncancellable', title='No cancel', cancellable=False)
def _uncancellable(ctx):
    return {}


@tasks.register('test_lock', title='Needs a lock')
def _needs_lock(ctx):
    with ctx.lock(ctx.params['key'], what='the test lock'):
        return {'locked': True}


@tasks.register('test_rollback', title='Rolls back')
def _rolls_back(ctx):
    raise tasks.TaskRolledBack('restart failed; previous config restored')


@pytest.fixture
def db():
    import app.main  # noqa: F401  (initializes the schema)
    from app.db.factory import get_database
    database = get_database()
    database.execute('DELETE FROM task_logs')
    database.execute('DELETE FROM tasks')
    database.commit()
    yield database
    database.rollback()
    database.execute('DELETE FROM task_logs')
    database.execute('DELETE FROM tasks')
    database.commit()


@pytest.fixture
def runner(db):
    import app.main as main
    r = tasks.TaskRunner(main._lock_connection, max_workers=2, worker_id='test-runner')
    yield r
    r.stop(wait=True)


def _logs(task_id):
    return [entry['line'] for entry in tasks.get_logs(task_id)]


# --- Runner ----------------------------------------------------------------------

def test_enqueue_claim_and_run(db, runner):
    row, created = tasks.enqueue('test_echo', {'value': 42, 'lines': ['hello', 'world']}, created_by='admin')
    assert created and row['status'] == 'queued'
    assert runner.run_once() == 'succeeded'
    done = tasks.get_task(row['id'])
    assert done['status'] == 'succeeded' and done['progress'] == 100
    assert json.loads(done['result']) == {'echo': 42}
    assert done['worker'] == 'test-runner' and done['started_at'] and done['finished_at']
    assert _logs(row['id']) == ['hello', 'world']
    assert tasks.get_logs(row['id'], after=1)[0]['line'] == 'world'


def test_step_markers_set_progress(db, runner):
    row, _ = tasks.enqueue('test_echo', {'lines': ['BACKUPX-STEP 2/4 Copying files', 'some output']})
    ctx = tasks.TaskContext(tasks.claim_one('w1'))
    for line in ctx.params['lines']:
        ctx.log(line)
    ctx.flush()
    current = tasks.get_task(row['id'])
    assert current['progress'] == 25
    assert current['step'] == 'Copying files'


def test_failed_and_rolled_back_status(db, runner):
    failed, _ = tasks.enqueue('test_fail')
    rolled, _ = tasks.enqueue('test_rollback')
    runner.run_once()
    runner.run_once()
    f = tasks.get_task(failed['id'])
    r = tasks.get_task(rolled['id'])
    assert f['status'] == 'failed' and f['error'] == 'it broke'
    assert r['status'] == 'rolled_back' and 'previous config restored' in r['error']


def test_idempotency_key_dedupes_active_tasks_only(db, runner):
    first, created1 = tasks.enqueue('test_echo', idempotency_key='same')
    second, created2 = tasks.enqueue('test_echo', idempotency_key='same')
    assert created1 and not created2 and first['id'] == second['id']
    runner.run_once()
    third, created3 = tasks.enqueue('test_echo', idempotency_key='same')
    assert created3 and third['id'] != first['id']


def test_concurrent_claims_never_hand_out_a_task_twice(db):
    ids = {tasks.enqueue('test_echo')[0]['id'] for _ in range(8)}
    claimed, errors = [], []

    def claim(n):
        try:
            while True:
                row = tasks.claim_one(f'w{n}')
                if not row:
                    break
                claimed.append(row['id'])
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            from app.db.factory import get_database
            get_database().close()

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(claimed) == sorted(ids)


def test_not_before_delays_claim(db):
    row, _ = tasks.enqueue('test_echo', not_before=tasks.utcnow() + timedelta(minutes=5))
    assert tasks.claim_one('w') is None
    db.execute('UPDATE tasks SET not_before = %s WHERE id = %s', (tasks.utcnow() - timedelta(seconds=1), row['id']))
    db.commit()
    assert tasks.claim_one('w')['id'] == row['id']


def test_cancel_queued_task(db, runner):
    row, _ = tasks.enqueue('test_echo')
    updated, error = tasks.request_cancel(row['id'])
    assert error is None and updated['status'] == 'cancelled'
    assert runner.run_once() is None


def test_cancel_running_task(db, runner):
    row, _ = tasks.enqueue('test_wait')
    assert runner.poll_once() == 1
    for _ in range(50):
        if tasks.get_task(row['id'])['status'] == 'running':
            break
        time.sleep(0.05)
    _, error = tasks.request_cancel(row['id'])
    assert error is None
    runner.heartbeat()  # turns cancel_requested into the in-process event
    for _ in range(100):
        if tasks.get_task(row['id'])['status'] != 'running':
            break
        time.sleep(0.05)
    assert tasks.get_task(row['id'])['status'] == 'cancelled'


def test_uncancellable_running_task_refuses_cancel(db):
    row, _ = tasks.enqueue('test_uncancellable')
    tasks.claim_one('w')
    _, error = tasks.request_cancel(row['id'])
    assert error and 'cannot be cancelled' in error
    assert tasks.get_task(row['id'])['cancel_requested'] is False


def test_stale_heartbeat_marks_task_interrupted(db):
    stale, _ = tasks.enqueue('test_echo')
    fresh, _ = tasks.enqueue('test_echo')
    tasks.claim_one('dead-worker')
    tasks.claim_one('live-worker')
    db.execute('UPDATE tasks SET heartbeat_at = %s WHERE id = %s',
               (tasks.utcnow() - timedelta(minutes=10), stale['id']))
    db.commit()
    assert tasks.mark_interrupted(stale_after=90) == 1
    s, f = tasks.get_task(stale['id']), tasks.get_task(fresh['id'])
    assert s['status'] == 'failed' and s['error'] == 'interrupted'
    assert f['status'] == 'running'


def test_heartbeat_touches_running_tasks(db, runner):
    row, _ = tasks.enqueue('test_wait')
    runner.poll_once()
    time.sleep(0.2)
    db.execute('UPDATE tasks SET heartbeat_at = %s WHERE id = %s',
               (tasks.utcnow() - timedelta(minutes=10), row['id']))
    db.commit()
    runner.heartbeat()
    assert tasks.get_task(row['id'])['heartbeat_at'] > tasks.utcnow() - timedelta(minutes=1)
    tasks.request_cancel(row['id'])
    runner.heartbeat()


def test_task_waits_for_or_fails_on_a_held_advisory_lock(db, runner):
    import app.main as main
    from app.scheduler.locks import job_run_lock
    row, _ = tasks.enqueue('test_lock', {'key': 'job-locked'})
    with job_run_lock(main._lock_connection, 'job-locked') as acquired:
        assert acquired
        assert runner.run_once() == 'failed'
    assert 'busy' in tasks.get_task(row['id'])['error']
    row2, _ = tasks.enqueue('test_lock', {'key': 'job-locked'})
    assert runner.run_once() == 'succeeded'


def test_repo_locks_shared_vs_exclusive(db):
    import app.main as main
    from app.scheduler.locks import repo_locks, repo_in_use, repo_lock_key
    url = 's3:https://s3.example.com/bucket/prefix'
    assert repo_lock_key(url) == repo_lock_key(url + '/')
    assert not repo_in_use(main._lock_connection, url)
    with repo_locks(main._lock_connection, [url]) as a:
        with repo_locks(main._lock_connection, [url]) as b:
            assert a and b  # shared: backup + restore side by side
            assert repo_in_use(main._lock_connection, url)
            with repo_locks(main._lock_connection, [url], exclusive=True) as c:
                assert not c
    assert not repo_in_use(main._lock_connection, url)


def test_params_and_logs_are_redacted(db, runner):
    row, _ = tasks.enqueue('test_echo', {
        'value': 1, 'password': 'hunter2-secret', 'nested': {'secret_key': 'abc'},
        'secret_value_for_test': None,
        'lines': ['AWS_SECRET_ACCESS_KEY=supersecret123 rest', 'url s3://user:pa55word@host/x']})
    stored = json.loads(tasks.get_task(row['id'])['params'])
    assert stored['password'] == '[REDACTED]' and stored['nested']['secret_key'] == '[REDACTED]'
    runner.run_once()
    text = '\n'.join(_logs(row['id']))
    assert 'supersecret123' not in text and 'pa55word' not in text
    assert 'AWS_SECRET_ACCESS_KEY=***' in text


def test_known_secret_values_are_redacted(db):
    row, _ = tasks.enqueue('test_echo')
    ctx = tasks.TaskContext(tasks.claim_one('w'))
    ctx.add_secret('Very$ecretValue')
    ctx.log('connecting with Very$ecretValue now')
    ctx.progress(10, 'using Very$ecretValue')
    ctx.flush()
    assert _logs(row['id']) == ['connecting with *** now']
    assert tasks.get_task(row['id'])['step'] == 'using ***'


def test_log_is_capped_but_keeps_the_tail(db, monkeypatch):
    monkeypatch.setenv('TASK_LOG_MAX_LINES', '100')
    monkeypatch.setenv('TASK_LOG_TAIL_LINES', '10')
    row, _ = tasks.enqueue('test_echo')
    ctx = tasks.TaskContext(tasks.claim_one('w'))
    for i in range(300):
        ctx.log(f'line {i}')
    tasks._finish(ctx, tasks.SUCCEEDED)
    lines = _logs(row['id'])
    assert len(lines) == 100 + 1 + 10
    assert lines[99] == 'line 99'
    assert 'omitted' in lines[100]
    assert lines[-1] == 'line 299'


def test_stream_logs_process_output_and_kills_on_cancel(db):
    row, _ = tasks.enqueue('test_echo')
    ctx = tasks.TaskContext(tasks.claim_one('w'))
    proc = subprocess.Popen(['sh', '-c', 'echo out; echo err >&2; echo "BACKUPX-STEP 1/2 half"; exit 3'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert ctx.stream(proc, timeout=30) == 3
    ctx.flush()
    entries = tasks.get_logs(row['id'])
    assert {(e['stream'], e['line']) for e in entries} >= {('stdout', 'out'), ('stderr', 'err')}

    proc = subprocess.Popen(['sleep', '30'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    threading.Timer(0.3, ctx.cancel_event.set).start()
    started = time.monotonic()
    with pytest.raises(tasks.TaskCancelled):
        ctx.stream(proc, timeout=60)
    assert time.monotonic() - started < 10
    assert proc.poll() is not None


def test_purge_old_removes_finished_tasks_and_logs(db, runner):
    row, _ = tasks.enqueue('test_echo', {'lines': ['x']})
    runner.run_once()
    db.execute('UPDATE tasks SET finished_at = %s WHERE id = %s', (tasks.utcnow() - timedelta(days=40), row['id']))
    db.commit()
    assert tasks.purge_old(keep_days=30) == 1
    assert tasks.get_task(row['id']) is None
    assert tasks.get_logs(row['id']) == []


# --- API -------------------------------------------------------------------------

@pytest.fixture
def api(client, monkeypatch, db):
    import app.main as main
    monkeypatch.setattr(main.limiter, 'enabled', False)
    assert client.post('/api/auth/login', json={'username': 'testadmin', 'password': 'testpassword123'}).status_code == 200
    return client


def test_task_api_list_get_logs_cancel(api, runner):
    row, _ = tasks.enqueue('test_echo', {'lines': ['a', 'b', 'c']})
    active = api.get('/api/tasks?active=1').get_json()['tasks']
    assert [t['id'] for t in active] == [row['id']]
    assert active[0]['status'] == 'queued' and active[0]['title'] == 'Echo'
    runner.run_once()
    assert api.get('/api/tasks?active=1').get_json()['tasks'] == []
    recent = api.get('/api/tasks?recent=60').get_json()['tasks']
    assert recent[0]['id'] == row['id'] and recent[0]['status'] == 'succeeded'
    got = api.get(f"/api/tasks/{row['id']}").get_json()
    assert got['result'] == {'echo': None} and got['log_seq'] == 3
    logs = api.get(f"/api/tasks/{row['id']}/logs?after=1").get_json()
    assert [l['line'] for l in logs['logs']] == ['b', 'c'] and logs['last_seq'] == 3
    assert api.post(f"/api/tasks/{row['id']}/cancel").status_code == 409
    assert api.get('/api/tasks/nope').status_code == 404
    queued, _ = tasks.enqueue('test_echo')
    response = api.post(f"/api/tasks/{queued['id']}/cancel")
    assert response.status_code == 200 and response.get_json()['task']['status'] == 'cancelled'


def test_task_api_requires_login(client):
    assert client.get('/api/tasks').status_code in (302, 401)


# --- CSRF ------------------------------------------------------------------------

@pytest.fixture
def csrf_on(app):
    app.config['WTF_CSRF_ENABLED'] = True
    yield
    app.config['WTF_CSRF_ENABLED'] = False


def test_mutating_routes_require_csrf_token(api, csrf_on):
    # Login stays exempt; everything else needs the header
    response = api.post('/api/tasks/nope/cancel')
    assert response.status_code == 400 and response.get_json()['code'] == 'csrf'
    response = api.post('/api/jobs', json={'name': 'x'})
    assert response.status_code == 400 and response.get_json()['code'] == 'csrf'
    token = api.get('/api/auth/csrf').get_json()['csrf_token']
    response = api.post('/api/tasks/nope/cancel', headers={'X-CSRFToken': token})
    assert response.status_code == 404  # past CSRF; the task just doesn't exist
    response = api.post('/api/tasks/nope/cancel', headers={'X-CSRFToken': 'forged'})
    assert response.status_code == 400


def test_login_is_csrf_exempt_and_returns_a_token(client, csrf_on, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.limiter, 'enabled', False)
    response = client.post('/api/auth/login', json={'username': 'testadmin', 'password': 'testpassword123'})
    assert response.status_code == 200 and response.get_json()['csrf_token']


def test_no_mutating_route_is_csrf_exempt_except_login():
    import inspect
    import app.main as main
    source = inspect.getsource(main)
    assert source.count('@csrf.exempt') == 1
    assert '@csrf.exempt\n@limiter.limit("5 per minute")  # Strict rate limiting for login' in source


# --- Step-up ---------------------------------------------------------------------

def test_step_up_flow(api, monkeypatch):
    assert api.get('/api/auth/step-up').get_json()['valid'] is False
    response = api.post('/api/jobs/job1/snapshots/abc/restore', json={'source_path': '/a', 'target_path': '/b'})
    assert response.status_code == 403 and response.get_json()['code'] == 'step_up_required'
    response = api.post('/api/auth/step-up', json={'password': 'wrong-password'})
    assert response.status_code == 401 and response.get_json()['code'] == 'step_up_failed'
    response = api.post('/api/auth/step-up', json={'password': 'testpassword123'})
    assert response.status_code == 200 and response.get_json()['valid'] is True
    assert api.get('/api/auth/step-up').get_json()['valid'] is True
    # Past the TTL it no longer counts
    with api.session_transaction() as sess:
        sess['step_up_at'] = time.time() - 301
    assert api.get('/api/auth/step-up').get_json()['valid'] is False
    # Logging out drops it
    api.post('/api/auth/step-up', json={'password': 'testpassword123'})
    api.post('/api/auth/logout')
    with api.session_transaction() as sess:
        assert 'step_up_at' not in sess


def test_step_up_is_audited(api, db):
    api.post('/api/auth/step-up', json={'password': 'wrong-password'})
    api.post('/api/auth/step-up', json={'password': 'testpassword123'})
    actions = [r['action'] for r in db.fetchall(
        "SELECT action FROM audit_log WHERE action LIKE 'STEP_UP%%' ORDER BY id DESC LIMIT 2")]
    assert actions == ['STEP_UP', 'STEP_UP_FAILED']


# --- Plans -----------------------------------------------------------------------

def test_plan_hash_is_stable_and_detects_changes():
    plan = {'action': 'restart', 'downtime_seconds': 30, 'steps': ['backup config', 'restart', 'check']}
    same = {'steps': ['backup config', 'restart', 'check'], 'downtime_seconds': 30, 'action': 'restart'}
    assert plan_hash(plan) == plan_hash(same)
    assert plan_hash(plan) != plan_hash(dict(plan, downtime_seconds=60))
    presented = present_plan(plan, confirm_text='epetition-db')
    assert presented['plan_hash'] == plan_hash(plan) and presented['confirm_text'] == 'epetition-db'


def test_verify_confirmation():
    plan = {'action': 'replace production', 'target': 'epetition'}
    h = plan_hash(plan)
    verify_confirmation(plan, h, ' epetition ', confirm_text='epetition')
    with pytest.raises(ConfirmationError) as e:
        verify_confirmation(plan, None)
    assert e.value.code == 'plan_hash_missing'
    with pytest.raises(ConfirmationError) as e:
        verify_confirmation(dict(plan, target='other'), h, 'epetition', confirm_text='epetition')
    assert e.value.code == 'plan_changed' and e.value.status == 409
    with pytest.raises(ConfirmationError) as e:
        verify_confirmation(plan, h, 'epetitio', confirm_text='epetition')
    assert e.value.code == 'confirmation_mismatch'


def test_check_confirmed_returns_new_plan_when_changed(app):
    from app.security import check_confirmed
    plan = {'a': 1}
    with app.test_request_context():
        assert check_confirmed(plan, {'plan_hash': plan_hash(plan), 'confirm': 'x'}, confirm_text='x') is None
        response, status = check_confirmed({'a': 2}, {'plan_hash': plan_hash(plan), 'confirm': 'x'}, confirm_text='x')
        body = response.get_json()
        assert status == 409 and body['code'] == 'plan_changed' and body['plan'] == {'a': 2}
        assert body['plan_hash'] == plan_hash({'a': 2})


# --- Run now / per-storage results / re-run failed storages ----------------------

TARGETS = [
    {'id': 'st1', 'name': 'primary', 'endpoint': 's3.example.com', 'bucket': 'b1', 'access_key': 'AK1',
     'secret_key': 'SK1-secret', 'skip_ssl_verify': False, 'status': 'active'},
    {'id': 'st2', 'name': 'offsite', 'endpoint': 'r2.example.com', 'bucket': 'b2', 'access_key': 'AK2',
     'secret_key': 'SK2-secret', 'skip_ssl_verify': False, 'status': 'active'},
]
JOB = {'id': 'jobrr', 'name': 'Run results job', 'backup_prefix': 'jobrr', 'restic_password': 'restic-pw-secret',
       'directories': ['/data'], 'excludes': [], 'backup_type': 'filesystem'}


@pytest.fixture
def quiet_job(monkeypatch, db):
    import app.main as main
    monkeypatch.setattr(main, 'get_job', lambda jid: dict(JOB) if jid == 'jobrr' else None)
    monkeypatch.setattr(main, 'get_job_storage_targets', lambda job, include_inactive=False: [dict(t) for t in TARGETS])
    for name in ('update_job_status', 'send_notification', 'invalidate_snapshot_cache', 'apply_retention'):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    db.execute("DELETE FROM run_results WHERE job_id = 'jobrr'")
    db.execute("DELETE FROM history WHERE job_id = 'jobrr'")
    db.commit()
    return main


def test_finish_backup_records_per_storage_results(quiet_job, db):
    main = quiet_job
    run = main.BackupRun('jobrr', trigger='manual')
    results = [{'exit_code': 0, 'output': ['ok line'], 'snapshot_id': 'snap1'},
               {'exit_code': 1, 'output': ['Fatal: unable to open repo', 'secret SK2-secret leaked']}]
    with main._backup_run_context(run):
        ok, message = main._finish_backup('jobrr', dict(JOB), [dict(t) for t in TARGETS], main.utc_now(), 1, '',
                                          ['preamble'], results, 'Backup')
    assert not ok
    rows = db.fetchall('SELECT * FROM run_results WHERE run_id = %s ORDER BY id', (run.run_id,))
    assert [(r['storage_id'], r['status'], r['exit_code'], r['snapshot_id']) for r in rows] == [
        ('st1', 'success', 0, 'snap1'), ('st2', 'failed', 1, None)]
    assert 'SK2-secret' not in (rows[1]['error'] or '')
    log = db.fetchone('SELECT content FROM run_logs WHERE id = %s', (rows[1]['log_id'],))['content']
    assert 'unable to open repo' in log and 'preamble' in log and 'SK2-secret' not in log
    hist = db.fetchone("SELECT run_id FROM history WHERE job_id = 'jobrr' ORDER BY id DESC LIMIT 1")
    assert hist['run_id'] == run.run_id


def test_runs_api_lists_failed_storages(api, quiet_job):
    main = quiet_job
    run = main.BackupRun('jobrr', trigger='scheduled')
    with main._backup_run_context(run):
        main._finish_backup('jobrr', dict(JOB), [dict(t) for t in TARGETS], main.utc_now(), 1, '', [],
                            [{'exit_code': 0, 'output': []}, {'exit_code': 3, 'output': ['boom']}], 'Backup')
    runs = api.get('/api/jobs/jobrr/runs').get_json()['runs']
    assert runs[0]['run_id'] == run.run_id
    assert runs[0]['status'] == 'partial' and runs[0]['failed_storage_ids'] == ['st2']
    log = api.get(f'/api/runs/{run.run_id}/log?storage_id=st2').get_json()
    assert 'boom' in log['content']
    assert api.get(f'/api/runs/{run.run_id}').get_json()['storages'][1]['status'] == 'failed'


def test_run_now_queues_a_task_and_validates_storage_ids(api, quiet_job):
    response = api.post('/api/jobs/jobrr/run', json={'storage_ids': ['nope']})
    assert response.status_code == 400
    response = api.post('/api/jobs/jobrr/run', json={'storage_ids': ['st2']})
    assert response.status_code == 202
    body = response.get_json()
    assert body['success'] and body['task']['kind'] == 'backup_run'
    assert body['task']['params'] == {'job_id': 'jobrr', 'trigger': 'rerun', 'storage_ids': ['st2']}
    again = api.post('/api/jobs/jobrr/run').get_json()
    assert again['deduplicated'] and again['task']['id'] == body['task']['id']


def test_rerun_backs_up_only_the_requested_storages(quiet_job, runner, monkeypatch):
    main = quiet_job
    seen = {}

    def fake_fs_backup(job_id, job, targets):
        seen['targets'] = [t['id'] for t in targets]
        run = main._current_run()
        seen['run'] = (run.trigger, run.task is not None)
        main.update_job_progress(job_id, 50, 'halfway')
        return True, 'Backup completed successfully'

    monkeypatch.setattr(main, 'run_filesystem_backup', fake_fs_backup)
    monkeypatch.setattr(main, 'update_job_progress', lambda *a, **kw: None)
    row, _ = tasks.enqueue('backup_run', {'job_id': 'jobrr', 'trigger': 'rerun', 'storage_ids': ['st2']})
    assert runner.run_once() == 'succeeded'
    assert seen == {'targets': ['st2'], 'run': ('rerun', True)}
    assert json.loads(tasks.get_task(row['id'])['result'])['message'] == 'Backup completed successfully'


def test_backup_task_fails_when_the_backup_fails(quiet_job, runner, monkeypatch):
    main = quiet_job
    monkeypatch.setattr(main, 'run_filesystem_backup', lambda job_id, job, targets: (False, 'offsite: boom'))
    row, _ = tasks.enqueue('backup_run', {'job_id': 'jobrr'})
    assert runner.run_once() == 'failed'
    assert tasks.get_task(row['id'])['error'] == 'offsite: boom'


# --- Operations moved into tasks ---------------------------------------------------

def test_restore_queues_task_and_audits(api, quiet_job, db, monkeypatch):
    main = quiet_job
    job = dict(JOB, server_id='srv1')
    monkeypatch.setattr(main, 'get_job', lambda jid: dict(job))
    monkeypatch.setattr(main, 'get_server', lambda sid: {'id': 'srv1', 'name': 'web1', 'host': 'web1.local'})
    monkeypatch.setattr(main, '_job_storage_from_request', lambda j: dict(TARGETS[0]))
    api.post('/api/auth/step-up', json={'password': 'testpassword123'})
    response = api.post('/api/jobs/jobrr/snapshots/abc123/restore', json={'source_path': '/data', 'target_path': '/r'})
    assert response.status_code == 202
    task = response.get_json()['task']
    assert task['kind'] == 'restore_files' and task['tier'] == 'confirm'
    assert task['params'] == {'job_id': 'jobrr', 'snapshot_id': 'abc123', 'storage_id': 'st1',
                              'source_path': '/data', 'target_path': '/r'}
    audit = db.fetchone("SELECT * FROM audit_log WHERE action = 'RESTORE_FILES' ORDER BY id DESC LIMIT 1")
    assert audit and json.loads(audit['changes'])['target_path'] == '/r'


def test_zip_download_runs_as_task_then_downloads(api, quiet_job, runner, monkeypatch, tmp_path):
    main = quiet_job
    monkeypatch.setenv('APP_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(main, 'resolve_job_storage', lambda job, sid=None: dict(TARGETS[0]))
    real_popen = subprocess.Popen

    def fake_restic(cmd, **kw):
        assert cmd[:2] == ['restic', 'restore']
        assert 'restic-pw-secret' not in ' '.join(cmd)
        assert kw['env']['RESTIC_PASSWORD'] == 'restic-pw-secret'
        target = cmd[cmd.index('--target') + 1]
        os.makedirs(os.path.join(target, 'data', 'sub'))
        open(os.path.join(target, 'data', 'a.txt'), 'w').write('hello')
        open(os.path.join(target, 'data', 'sub', 'b.txt'), 'w').write('world')
        return real_popen(['sh', '-c', 'echo restored 2 files'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)

    monkeypatch.setattr(main.subprocess, 'Popen', fake_restic)
    response = api.post('/api/jobs/jobrr/snapshots/abcdef12/zip', json={'path': '/data'})
    assert response.status_code == 202
    task_id = response.get_json()['task']['id']
    assert runner.run_once() == 'succeeded'
    result = api.get(f'/api/tasks/{task_id}').get_json()['result']
    assert result['filename'] == 'data.zip' and result['files'] == 2
    download = api.get(result['download_url'])
    assert download.status_code == 200 and download.data[:2] == b'PK'
    assert not os.path.exists(os.path.join(str(tmp_path), 'task-files', f'{task_id}.d'))


def test_init_repo_and_install_restic_and_pitr_ship_are_tasks(api, quiet_job, monkeypatch):
    main = quiet_job
    response = api.post('/api/jobs/jobrr/init')
    assert response.status_code == 202 and response.get_json()['task']['kind'] == 'init_repo'
    monkeypatch.setattr(main, 'get_server', lambda sid: {'id': 's1', 'name': 's', 'host': 'h', 'ssh_user': 'root'})
    response = api.post('/api/servers/s1/install-restic')
    assert response.status_code == 202 and response.get_json()['task']['kind'] == 'install_restic'
    monkeypatch.setattr(main, 'get_job', lambda jid: dict(JOB, pitr_enabled=True))
    response = api.post('/api/jobs/jobrr/pitr/ship')
    assert response.status_code == 202 and response.get_json()['task']['kind'] == 'pitr_ship'


def test_init_repo_task_reports_per_storage(quiet_job, runner, monkeypatch):
    main = quiet_job
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get('env', {}).get('AWS_SECRET_ACCESS_KEY')))
        return subprocess.CompletedProcess(cmd, 0 if len(calls) == 1 else 1, stdout='',
                                           stderr='' if len(calls) == 1 else 'config file already exists')

    monkeypatch.setattr(main.subprocess, 'run', fake_run)
    row, _ = tasks.enqueue('init_repo', {'job_id': 'jobrr'})
    assert runner.run_once() == 'succeeded'
    assert [c[1] for c in calls] == ['SK1-secret', 'SK2-secret']
    assert 'already initialized' in json.loads(tasks.get_task(row['id'])['result'])['message']


# --- known_hosts ---------------------------------------------------------------------

def test_ssh_uses_writable_known_hosts(monkeypatch, tmp_path):
    import app.main as main
    path = tmp_path / 'known_hosts'
    monkeypatch.setenv('SSH_KNOWN_HOSTS_FILE', str(path))
    mounted = tmp_path / 'mounted_known_hosts'
    mounted.write_text('host1 ssh-ed25519 AAAA1\n# comment\nhost2 ssh-ed25519 AAAA2\n')
    monkeypatch.setattr(main, '_mounted_known_hosts_files', lambda: [str(mounted)])
    monkeypatch.setitem(main._known_hosts_ready, 'path', None)
    path.write_text('host1 ssh-ed25519 AAAA1\n')
    assert main.seed_known_hosts() == str(path)
    assert path.read_text().splitlines() == ['host1 ssh-ed25519 AAAA1', 'host2 ssh-ed25519 AAAA2']
    cmd = main._build_ssh_cmd('h', 'u', 22, ssh_key='/k')
    assert f'UserKnownHostsFile={path}' in cmd
    assert 'StrictHostKeyChecking=accept-new' in cmd
    main.seed_known_hosts()  # idempotent
    assert len(path.read_text().splitlines()) == 2

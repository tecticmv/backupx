"""
Health checks and fixes (app/health.py pure detectors, app/health_service.py
DB/probes/fixes, the /api/health, /api/fixes and dashboard routes).

Detectors run on hand-built state snapshots; the service tests use the test
Postgres with real job/server/storage rows (ids prefixed 'hx'). restic, SSH
and ssh-keyscan are replaced with fakes.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import health, tasks
from app.health import Issue, lock_decision

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def base_state(**over):
    state = {
        'now': NOW,
        'jobs': {'j1': {'id': 'j1', 'name': 'Files', 'status': 'success', 'server_id': 's1',
                        'schedule_enabled': True, 'schedule_cron': '0 2 * * *'}},
        'job_storages': {'j1': [{'id': 'a', 'name': 'Primary', 'repo_url': 's3:https://a/b/j1'},
                                {'id': 'b', 'name': 'Offsite', 'repo_url': 's3:https://r2/b/j1'}]},
        'latest_results': {},
        'retention': {},
        'pitr': {},
        'pitr_heartbeat_max_age': 300,
        'pg_conf_changes': ['archive_mode = on'],
        'scheduler': {'heartbeat_at': _ago(seconds=10), 'max_age': 120},
        'channels': [],
        'servers': {'s1': {'id': 's1', 'name': 'web1', 'host': 'web1.lan', 'ssh_port': 22}},
        'servers_in_use': {'s1'},
        'probes': {},
    }
    state.update(over)
    return state


def kinds(issues):
    return sorted(i.kind for i in issues)


# --- Detectors ---------------------------------------------------------------------

def test_healthy_state_has_no_issues():
    assert health.detect(base_state()) == []


def test_overdue_job_is_critical_with_run_now_fix():
    state = base_state()
    state['jobs']['j1']['overdue_since'] = _ago(hours=8)
    [issue] = health.detect(state)
    assert issue.kind == 'job_overdue' and issue.severity == 'critical' and issue.fix == 'run_now'
    assert issue.alert is True


def test_partial_failure_is_warning_and_retries_only_failed_storages():
    state = base_state(latest_results={'j1': {
        'a': {'status': 'success', 'run_id': 'r1'},
        'b': {'status': 'failed', 'error': 'Fatal: unable to save snapshot: file already closed', 'run_id': 'r1',
              'finished_at': _ago(minutes=5)}}})
    [issue] = health.detect(state)
    assert issue.kind == 'backup_failed' and issue.severity == 'warning' and issue.fix == 'rerun_failed'
    assert issue.evidence['failed_storages'] == 'Offsite' and issue.evidence['run_id'] == 'r1'
    assert issue.alert is False  # the backup failure itself already notified


def test_all_storages_unreachable_is_critical_with_retry_fix():
    state = base_state(latest_results={'j1': {
        'a': {'status': 'failed', 'error': 'Save(<lock/1>) returned error: 524 Origin Time-out'},
        'b': {'status': 'failed', 'error': 'dial tcp 10.0.0.1:443: i/o timeout'}}})
    [issue] = health.detect(state)
    assert issue.kind == 'storage_unreachable' and issue.severity == 'critical' and issue.fix == 'storage_retry'


def test_issue_id_is_stable_across_variants():
    unreachable = base_state(latest_results={'j1': {'a': {'status': 'failed', 'error': '503 Service Unavailable'}}})
    other = base_state(latest_results={'j1': {'a': {'status': 'failed', 'error': 'something odd'}}})
    [i1], [i2] = health.detect(unreachable), health.detect(other)
    assert i1.kind != i2.kind and i1.id == i2.id
    assert i1.id == health.issue_id('backup_failed', 'j1')


def test_wrong_password_is_manual_not_rerun():
    state = base_state(latest_results={'j1': {
        'a': {'status': 'success'},
        'b': {'status': 'failed', 'error': 'Fatal: wrong password or no key found'}}})
    [issue] = health.detect(state)
    assert issue.kind == 'repo_open_error' and issue.fix is None and issue.manual['steps']


def test_running_job_is_not_flagged():
    state = base_state(latest_results={'j1': {'a': {'status': 'failed', 'error': 'x'}}})
    state['jobs']['j1']['status'] = 'running'
    assert health.detect(state) == []


def test_retention_failed_vs_refused():
    state = base_state(retention={'j1': [
        {'storage_id': 'a', 'storage_name': 'Primary', 'action': 'forget', 'status': 'failed',
         'message': 'unable to create lock in backend: repository is already locked', 'timestamp': _ago(hours=1)},
        {'storage_id': 'b', 'storage_name': 'Offsite', 'action': 'forget', 'status': 'failed',
         'message': 'refused, nothing removed: group host=x would lose all snapshots', 'timestamp': _ago(hours=1)}]})
    issues = {i.kind: i for i in health.detect(state)}
    assert issues['retention_failed'].fix == 'retry_forget'
    assert issues['retention_refused'].fix is None and issues['retention_refused'].manual


def test_pitr_streamer_down_and_unanchored():
    state = base_state()
    state['jobs']['j1'].update(pitr_enabled=True, backup_type='database')
    state['pitr'] = {'j1': {'kind': 'mysql', 'state': 'lagging', 'last_tick_at': _ago(minutes=2),
                            'streamer_active': 'failed', 'heartbeat': _ago(hours=1)}}
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_streamer_down' and issue.severity == 'critical' and issue.fix == 'pitr_kick'

    state['pitr']['j1'].update(state='unanchored', streamer_active='active', heartbeat=_ago(seconds=30))
    state['probes'] = {'prereq:j1': {'data': {'ok': True}, 'checked_at': _ago(minutes=5)}}
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_unanchored' and issue.fix == 'anchored_dump'
    # Prerequisites failing: no anchored dump offered, manual steps instead
    state['probes'] = {'prereq:j1': {'data': {'ok': False}, 'checked_at': _ago(minutes=5)}}
    [issue] = health.detect(state)
    assert issue.fix is None and issue.manual


def test_pitr_client_image_mismatch_and_wal_issues():
    state = base_state()
    state['jobs']['j1'].update(pitr_enabled=True, backup_type='database')
    state['pitr'] = {'j1': {'kind': 'mysql', 'state': 'ok', 'last_tick_at': _ago(minutes=2),
                            'streamer_active': 'active', 'heartbeat': _ago(seconds=30)}}
    state['probes'] = {'prereq:j1': {'data': {'ok': True, 'image_mismatch': {
        'current': '', 'suggested': 'percona/percona-server:8.4', 'server_version': '8.4.3'}}, 'checked_at': NOW}}
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_client_image' and issue.fix == 'pitr_client_image'

    pg = base_state()
    pg['jobs']['j1'].update(pitr_enabled=True, backup_type='database')
    pg['pitr'] = {'j1': {'kind': 'postgres', 'state': 'lagging', 'last_tick_at': _ago(minutes=2),
                         'streamer_active': 'failing',
                         'info': {'mirror': [{'storage': 'Offsite', 'ok': False, 'error': '403'}]}}}
    assert kinds(health.detect(pg)) == ['wal_archive_failing', 'wal_mirror_failed']
    pg['pitr']['j1'].update(streamer_active='inactive', info={}, state='pending')
    [issue] = health.detect(pg)
    assert issue.kind == 'wal_archiving_off' and issue.manual['commands'] == ['archive_mode = on']


def test_scheduler_heartbeat():
    assert health.detect(base_state(scheduler={'heartbeat_at': _ago(seconds=60), 'max_age': 120})) == []
    [issue] = health.detect(base_state(scheduler={'heartbeat_at': _ago(minutes=10), 'max_age': 120}))
    assert issue.kind == 'scheduler_down' and issue.severity == 'critical' and issue.manual
    [issue] = health.detect(base_state(scheduler={'heartbeat_at': None, 'max_age': 120}))
    assert issue.evidence['age'] == 'never'


def test_notification_channel_failing_until_a_later_success():
    ch = {'id': 'c1', 'name': 'Ops', 'type': 'telegram', 'enabled': True,
          'last_error': 'HTTP Error 401', 'last_error_at': _ago(hours=1), 'last_success_at': _ago(hours=2)}
    [issue] = health.detect(base_state(channels=[ch]))
    assert issue.kind == 'notification_failing' and issue.fix == 'notification_test'
    assert health.detect(base_state(channels=[dict(ch, last_success_at=_ago(minutes=5))])) == []


def test_server_probe_issues():
    probes = {'ssh:s1': {'data': {'ok': False, 'error': 'Connection timed out'}, 'checked_at': NOW}}
    [issue] = health.detect(base_state(probes=probes))
    assert issue.kind == 'ssh_unreachable' and issue.severity == 'critical' and issue.fix == 'ssh_retest'
    probes['ssh:s1']['data'] = {'ok': False, 'host_key_changed': True,
                                'error': '@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@'}
    [issue] = health.detect(base_state(probes=probes))
    assert issue.kind == 'host_key_changed' and issue.fix == 'trust_host_key'
    probes['ssh:s1']['data'] = {'ok': True, 'restic_missing': True,
                                'disks': [{'mount': '/', 'use_pct': '95%', 'avail': '1 GB'},
                                          {'mount': '/var', 'use_pct': '40%', 'avail': '9 GB'}]}
    issues = {i.kind: i for i in health.detect(base_state(probes=probes))}
    assert set(issues) == {'restic_missing', 'low_disk'}
    assert issues['low_disk'].severity == 'info' and issues['low_disk'].manual['commands']
    assert issues['restic_missing'].fix == 'install_restic'


def test_repo_probe_issues():
    lock_auto = {'id': 'a' * 64, 'time': _ago(hours=2), 'hostname': 'web1', 'pid': 42, 'decision': 'auto'}
    lock_foreign = {'id': 'b' * 64, 'time': _ago(hours=2), 'hostname': 'laptop', 'pid': 7, 'decision': 'confirm'}
    lock_live = {'id': 'c' * 64, 'time': _ago(minutes=2), 'hostname': 'web1', 'pid': 9, 'decision': 'wait'}
    probes = {
        'repo:j1:a': {'data': {'open': 'ok', 'locks': [lock_auto, lock_foreign, lock_live],
                               'keys': [{'id': 'k1', 'current': True}, {'id': 'k2' * 10, 'current': False,
                                                                        'userName': 'bob', 'hostName': 'pc'}]},
                      'checked_at': NOW},
        'repo:j1:b': {'data': {'open': 'not_initialized'}, 'checked_at': NOW},
        'check:j1:a': {'data': {'ok': False, 'error': 'Fatal: repository contains errors'}, 'checked_at': NOW},
    }
    issues = health.detect(base_state(probes=probes))
    assert kinds(issues) == ['repo_check_failed', 'repo_not_initialized', 'stale_lock', 'stale_lock_foreign',
                             'stray_key']
    by_kind = {i.kind: i for i in issues}
    assert by_kind['stale_lock'].fix == 'unlock_stale'          # safe
    assert by_kind['stale_lock_foreign'].fix == 'unlock_lock'   # confirm
    assert by_kind['stray_key'].fix == 'remove_key' and by_kind['repo_check_failed'].fix == 'repair_index'
    assert by_kind['repo_not_initialized'].fix == 'init_repo'


# --- Stale-lock rules --------------------------------------------------------------

MANAGED = {'web1.lan', 'web1'}
BACKUPX = {'backupx-7f3a'}


def test_live_lock_is_never_auto():
    lock = {'time': _ago(minutes=29), 'hostname': 'web1'}
    assert lock_decision(lock, NOW, MANAGED, BACKUPX, False, False)[0] == 'wait'


def test_old_lock_from_managed_host_or_container_is_auto():
    assert lock_decision({'time': _ago(minutes=31), 'hostname': 'WEB1'}, NOW, MANAGED, BACKUPX, False, False)[0] == 'auto'
    assert lock_decision({'time': _ago(hours=5), 'hostname': 'backupx-7f3a'}, NOW, MANAGED, BACKUPX,
                         False, False)[0] == 'auto'


def test_lock_with_backupx_activity_waits():
    lock = {'time': _ago(hours=5), 'hostname': 'web1'}
    assert lock_decision(lock, NOW, MANAGED, BACKUPX, True, False)[0] == 'wait'   # job / pitr / prune lock held
    assert lock_decision(lock, NOW, MANAGED, BACKUPX, False, True)[0] == 'wait'   # repo_in_use()


def test_foreign_host_lock_needs_confirmation():
    decision, reason = lock_decision({'time': _ago(hours=5), 'hostname': 'laptop'}, NOW, MANAGED, BACKUPX,
                                     False, False)
    assert decision == 'confirm' and 'laptop' in reason
    assert lock_decision({'time': None, 'hostname': 'web1'}, NOW, MANAGED, BACKUPX, False, False)[0] == 'wait'


def test_restic_nanosecond_timestamps_parse():
    assert health.utc('2026-11-01T11:00:00.123456789+05:00') == datetime(2026, 11, 1, 6, 0, 0, 123456,
                                                                          tzinfo=timezone.utc)


# --- Alerts (pure) -----------------------------------------------------------------

def test_alert_actions():
    base = {'severity': 'critical', 'alert': True, 'alerted_at': None, 'resolved_at': None,
            'resolved_alerted_at': None, 'snoozed_until': None}
    assert [a for a, _ in health.alert_actions([base], NOW)] == ['new']
    assert health.alert_actions([dict(base, severity='warning')], NOW) == []
    assert health.alert_actions([dict(base, alert=False)], NOW) == []
    assert health.alert_actions([dict(base, alerted_at=NOW)], NOW) == []
    assert health.alert_actions([dict(base, snoozed_until=NOW + timedelta(hours=1))], NOW) == []
    assert [a for a, _ in health.alert_actions([dict(base, alerted_at=NOW, resolved_at=NOW)], NOW)] == ['resolved']
    # Resolved before it was ever alerted: no "resolved" message
    assert health.alert_actions([dict(base, resolved_at=NOW)], NOW) == []


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
        for sql in ("DELETE FROM health_findings", "DELETE FROM health_probes",
                    "DELETE FROM task_logs", "DELETE FROM tasks",
                    "DELETE FROM run_results WHERE job_id LIKE 'hx%%'",
                    "DELETE FROM retention_runs WHERE job_id LIKE 'hx%%'",
                    "DELETE FROM jobs WHERE id LIKE 'hx%%'", "DELETE FROM servers WHERE id LIKE 'hx%%'",
                    "DELETE FROM s3_configs WHERE id LIKE 'hx%%'",
                    "DELETE FROM notification_channels WHERE id LIKE 'hx%%'"):
            database.execute(sql)
        database.commit()
    clean()
    yield database
    clean()


@pytest.fixture
def world(db, monkeypatch):
    """One server, two storages, one job; restic/ssh faked."""
    import app.main as main
    main.create_server({'id': 'hxsrv', 'name': 'hx-web', 'host': 'hx-web.lan', 'ssh_port': 22, 'ssh_user': 'root',
                        'ssh_auth_type': 'password', 'ssh_password': 'Ssh-Secret-pw1'})
    for sid, name in (('hxs1', 'HX Primary'), ('hxs2', 'HX Offsite')):
        main.create_s3_config({'id': sid, 'name': name, 'endpoint': f'{sid}.example.com', 'bucket': 'bk',
                               'access_key': f'AK-{sid}', 'secret_key': f'SK-{sid}-secret'})
    main.save_job('hxjob', {'name': 'HX Job', 'backup_type': 'filesystem', 'server_id': 'hxsrv',
                            's3_config_id': 'hxs1', 's3_config_ids': ['hxs1', 'hxs2'], 'directories': ['/srv'],
                            'restic_password': 'Restic-Secret-pw1', 'backup_prefix': 'hxjob',
                            'schedule_enabled': True, 'schedule_cron': '0 3 * * *'})
    from app import health_service as hs
    hs.record_scheduler_heartbeat()
    monkeypatch.setattr(main.limiter, 'enabled', False)
    return main


@pytest.fixture
def runner(db):
    import app.main as main
    r = tasks.TaskRunner(main._lock_connection, max_workers=2, worker_id='test-health')
    yield r
    r.stop(wait=True)


@pytest.fixture
def api(client, world):
    assert client.post('/api/auth/login', json={'username': 'testadmin', 'password': 'testpassword123'}).status_code == 200
    return client


def _fail_storage(db, job_id, storage_id, status='failed', error='Fatal: boom', run_id='hxrun1'):
    db.execute('INSERT INTO run_results (run_id, job_id, storage_id, storage_name, status, exit_code, error, '
               'trigger, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s)',
               (run_id, job_id, storage_id, storage_id, status, error, 'scheduled', tasks.utcnow(), tasks.utcnow()))
    db.commit()


def _issues(kind=None):
    from app import health_service as hs
    out = [i for i in hs.current_issues() if i.resource.get('job_id') == 'hxjob' or
           i.resource.get('id') in ('hxsrv', 'hxch')]
    return [i for i in out if kind is None or i.kind == kind]


def test_api_health_lists_issues_and_records_first_seen(api, db):
    _fail_storage(db, 'hxjob', 'hxs2')
    body = api.get('/api/health?resource_type=job&resource_id=hxjob').get_json()
    [issue] = body['issues']
    assert issue['kind'] == 'backup_failed' and issue['fix']['tier'] == 'safe'
    assert issue['fix']['action'] == 'rerun_failed' and issue['resource']['job_name'] == 'HX Job'
    assert body['summary']['safe_fixable'] >= 1 and 'last_sweep_at' in body['summary']
    first = issue['first_seen']
    again = api.get('/api/health?resource_type=job&resource_id=hxjob').get_json()['issues'][0]
    assert again['first_seen'] == first and again['id'] == issue['id']
    row = db.fetchone('SELECT * FROM health_findings WHERE issue_id = %s', (issue['id'],))
    assert row and row['resolved_at'] is None


def test_safe_fix_queues_and_reruns_only_failed_storages(api, db, runner, monkeypatch):
    import app.main as main
    _fail_storage(db, 'hxjob', 'hxs2')
    [issue] = _issues('backup_failed')
    seen = {}

    def fake_run_backup(job_id, storage_ids=None, trigger='scheduled', task=None, run_id=None):
        seen.update(job_id=job_id, storage_ids=storage_ids, trigger=trigger)
        return True, 'Backup completed successfully'
    monkeypatch.setattr(main, 'run_backup', fake_run_backup)
    r1 = api.post('/api/fixes', json={'issue_id': issue.id, 'idempotency_key': 'click-1'})
    assert r1.status_code == 202 and r1.get_json()['task']['kind'] == 'fix.rerun_failed'
    # Idempotent: a second click (or tab) returns the same active task
    r2 = api.post('/api/fixes', json={'issue_id': issue.id, 'idempotency_key': 'click-2'})
    assert r2.get_json()['deduplicated'] and r2.get_json()['task']['id'] == r1.get_json()['task']['id']
    assert runner.run_once() == 'succeeded'
    assert seen == {'job_id': 'hxjob', 'storage_ids': ['hxs2'], 'trigger': 'fix'}
    audit = db.fetchall("SELECT action, status FROM audit_log WHERE action LIKE 'HEALTH_FIX%%' ORDER BY id DESC LIMIT 2")
    assert [a['action'] for a in audit] == ['HEALTH_FIX_DONE', 'HEALTH_FIX']
    params = r1.get_json()['task']['params']
    assert 'Restic-Secret-pw1' not in json.dumps(params) and 'SK-hxs2-secret' not in json.dumps(params)


def test_fix_rejected_when_issue_went_away(api, db, runner):
    _fail_storage(db, 'hxjob', 'hxs2')
    [issue] = _issues('backup_failed')
    r = api.post('/api/fixes', json={'issue_id': issue.id})
    assert r.status_code == 202
    _fail_storage(db, 'hxjob', 'hxs2', status='success', error=None, run_id='hxrun2')  # resolved meanwhile
    assert runner.run_once() == 'succeeded'
    task = tasks.get_task(r.get_json()['task']['id'])
    assert json.loads(task['result'])['skipped'] is True
    assert api.post('/api/fixes', json={'issue_id': issue.id}).get_json()['code'] == 'issue_resolved'


def test_manual_issue_cannot_be_fixed(api, db):
    _fail_storage(db, 'hxjob', 'hxs2', error='Fatal: wrong password or no key found')
    [issue] = _issues('repo_open_error')
    r = api.post('/api/fixes', json={'issue_id': issue.id})
    assert r.status_code == 400 and r.get_json()['code'] == 'manual'


@pytest.fixture
def host_key_issue(world, db, monkeypatch):
    from app import health_service as hs
    hs.save_probe('ssh:hxsrv', {'ok': False, 'host_key_changed': True,
                                'error': 'WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!'})
    keys = {'new': ['ED25519 SHA256:newnewnew']}
    monkeypatch.setattr(hs, 'known_host_fingerprints', lambda host, port: ['ED25519 SHA256:oldoldold'])
    monkeypatch.setattr(hs, 'scan_host_keys', lambda host, port: (['hx-web.lan ssh-ed25519 AAAAnew'], keys['new']))
    [issue] = _issues('host_key_changed')
    return issue, keys


def test_confirm_fix_needs_preview_step_up_hash_and_phrase(api, host_key_issue, runner, monkeypatch, tmp_path):
    from app import health_service as hs
    import app.main as main
    issue, keys = host_key_issue
    preview = api.post('/api/fixes', json={'issue_id': issue.id, 'preview': True}).get_json()
    assert preview['fix']['tier'] == 'confirm' and preview['confirm_text'] == 'hx-web'
    assert preview['plan']['details']['old_fingerprints'] == ['ED25519 SHA256:oldoldold']
    assert preview['plan']['details']['new_fingerprints'] == ['ED25519 SHA256:newnewnew']
    body = {'issue_id': issue.id, 'plan_hash': preview['plan_hash'], 'confirm': 'hx-web'}
    # Step-up required
    r = api.post('/api/fixes', json=body)
    assert r.status_code == 403 and r.get_json()['code'] == 'step_up_required'
    assert api.post('/api/auth/step-up', json={'password': 'testpassword123'}).status_code == 200
    # Hash missing / wrong phrase
    r = api.post('/api/fixes', json={'issue_id': issue.id, 'confirm': 'hx-web'})
    assert r.status_code == 400 and r.get_json()['code'] == 'plan_hash_missing'
    r = api.post('/api/fixes', json=dict(body, confirm='hx-we'))
    assert r.status_code == 400 and r.get_json()['code'] == 'confirmation_mismatch'
    # The server key changed again after the preview: plan changed
    keys['new'] = ['ED25519 SHA256:evilevil']
    r = api.post('/api/fixes', json=body)
    assert r.status_code == 409 and r.get_json()['code'] == 'plan_changed'
    assert r.get_json()['plan']['details']['new_fingerprints'] == ['ED25519 SHA256:evilevil']
    keys['new'] = ['ED25519 SHA256:newnewnew']
    r = api.post('/api/fixes', json=body)
    assert r.status_code == 202 and r.get_json()['task']['tier'] == 'confirm'
    # Between confirm and execution the key changes: the task re-derives the plan and refuses
    keys['new'] = ['ED25519 SHA256:evilevil']
    kh = tmp_path / 'known_hosts'
    kh.write_text('hx-web.lan ssh-ed25519 AAAAold\n')
    monkeypatch.setattr(main, 'known_hosts_path', lambda: str(kh))
    assert runner.run_once() == 'failed'
    task = tasks.get_task(r.get_json()['task']['id'])
    assert 'State changed' in task['error']
    assert kh.read_text() == 'hx-web.lan ssh-ed25519 AAAAold\n'


def test_trust_host_key_rewrites_known_hosts(api, host_key_issue, runner, monkeypatch, tmp_path):
    from app import health_service as hs
    import app.main as main
    issue, keys = host_key_issue
    kh = tmp_path / 'known_hosts'
    kh.write_text('other.lan ssh-ed25519 AAAAother\nhx-web.lan ssh-ed25519 AAAAold\n')
    monkeypatch.setattr(main, 'known_hosts_path', lambda: str(kh))
    monkeypatch.setattr(hs, 'probe_server', lambda server: {'ok': True})
    api.post('/api/auth/step-up', json={'password': 'testpassword123'})
    preview = api.post('/api/fixes', json={'issue_id': issue.id, 'preview': True}).get_json()
    r = api.post('/api/fixes', json={'issue_id': issue.id, 'plan_hash': preview['plan_hash'], 'confirm': 'hx-web'})
    assert r.status_code == 202
    assert runner.run_once() == 'succeeded'
    text = kh.read_text()
    assert 'AAAAold' not in text and 'AAAAother' in text and 'hx-web.lan ssh-ed25519 AAAAnew' in text


def test_fix_all_safe_preview_excludes_confirm_tier(api, db, host_key_issue, monkeypatch, runner):
    import app.main as main
    _fail_storage(db, 'hxjob', 'hxs2')
    preview = api.post('/api/fixes/all-safe', json={'preview': True}).get_json()
    actions = {f['action'] for f in preview['fixes']}
    assert 'rerun_failed' in actions and 'trust_host_key' not in actions
    rerun = next(f for f in preview['fixes'] if f['action'] == 'rerun_failed')
    calls = []
    monkeypatch.setattr(main, 'run_backup', lambda job_id, **kw: (calls.append(kw.get('storage_ids')) or
                                                                  (True, 'Backup completed successfully')))
    # Even if a confirm-tier issue id is smuggled in, the parent never runs it
    r = api.post('/api/fixes/all-safe', json={'issue_ids': [rerun['issue_id'], host_key_issue[0].id]})
    assert r.status_code == 202 and r.get_json()['task']['kind'] == 'fix.all_safe'
    parent_id = r.get_json()['task']['id']
    t = __import__('threading').Thread(target=runner.run_once)
    t.start()
    deadline = time.time() + 20
    while time.time() < deadline and tasks.get_task(parent_id)['status'] in ('queued', 'running'):
        runner.run_once()
        time.sleep(0.2)
    t.join(timeout=10)
    parent = tasks.get_task(parent_id)
    assert parent['status'] == 'succeeded', parent
    children = db.fetchall('SELECT kind, status FROM tasks WHERE parent_id = %s', (parent_id,))
    assert [(c['kind'], c['status']) for c in children] == [('fix.rerun_failed', 'succeeded')]
    assert calls == [['hxs2']]
    assert json.loads(parent['result'])['skipped'] == 1


def test_snooze_hides_issue_and_blocks_alert(api, db):
    _fail_storage(db, 'hxjob', 'hxs2')
    [issue] = api.get('/api/health?resource_type=job&resource_id=hxjob').get_json()['issues']
    r = api.post(f"/api/issues/{issue['id']}/snooze", json={'hours': 24})
    assert r.status_code == 200 and r.get_json()['issue']['snoozed_until']
    assert api.get('/api/health?resource_type=job&resource_id=hxjob').get_json()['issues'] == []
    shown = api.get('/api/health?resource_type=job&resource_id=hxjob&include_snoozed=1').get_json()['issues']
    assert shown[0]['snoozed_until']
    assert api.post(f"/api/issues/{issue['id']}/snooze", json={'hours': 0}).get_json()['issue']['snoozed_until'] is None
    assert api.post(f"/api/issues/{issue['id']}/snooze", json={'hours': 9999}).status_code == 400
    assert api.post('/api/issues/nope/snooze', json={'hours': 1}).status_code == 404


def test_critical_alert_dedup_and_resolved_message(world, db, monkeypatch):
    import app.main as main
    from app import health_service as hs
    main.create_notification_channel({'id': 'hxch', 'name': 'HX TG', 'type': 'telegram', 'enabled': True,
                                      'config': {'bot_token': '123:SECRETTOKEN', 'chat_id': '1'},
                                      'notify_on_success': False, 'notify_on_failure': True})
    sent = []
    monkeypatch.setattr(main, '_dispatch_notification', lambda ch, name, status, msg, d: sent.append((status, msg)))
    hs.save_probe('ssh:hxsrv', {'ok': False, 'error': 'Connection refused'})
    hs.sync_findings(hs.current_issues())
    hs.send_alerts()
    hs.sync_findings(hs.current_issues())
    hs.send_alerts()
    mine = [s for s in sent if 'hx-web' in s[1]]
    assert len(mine) == 1 and mine[0][0] == 'failed' and 'SSH' in mine[0][1]
    hs.save_probe('ssh:hxsrv', {'ok': True})
    hs.sync_findings(hs.current_issues())
    hs.send_alerts()
    hs.send_alerts()
    mine = [s for s in sent if 'hx-web' in s[1]]
    assert [s[0] for s in mine] == ['failed', 'recovered'] and mine[1][1].startswith('Resolved')
    # It comes back: a new alert
    hs.save_probe('ssh:hxsrv', {'ok': False, 'error': 'Connection refused'})
    hs.sync_findings(hs.current_issues())
    hs.send_alerts()
    assert [s[0] for s in sent if 'hx-web' in s[1]] == ['failed', 'recovered', 'failed']


def test_backup_failure_issue_never_alerts_twice_via_health(world, db, monkeypatch):
    import app.main as main
    from app import health_service as hs
    sent = []
    monkeypatch.setattr(main, '_dispatch_notification', lambda *a: sent.append(a))
    monkeypatch.setattr(hs, '_alert_channels', lambda: [{'name': 'x', 'type': 'telegram'}])
    _fail_storage(db, 'hxjob', 'hxs1')
    _fail_storage(db, 'hxjob', 'hxs2')
    hs.sync_findings(hs.current_issues())
    hs.send_alerts()
    assert not [a for a in sent if 'HX Job' in a[3]]  # the run already sent its failure notification


def test_notification_errors_are_recorded_redacted(world, db, monkeypatch):
    import app.main as main
    main.create_notification_channel({'id': 'hxch', 'name': 'HX TG', 'type': 'telegram', 'enabled': True,
                                      'config': {'bot_token': '123:SECRETTOKEN', 'chat_id': '1'},
                                      'notify_on_success': True, 'notify_on_failure': True})

    def boom(channel, *a):
        raise RuntimeError('POST https://api.telegram.org/bot123:SECRETTOKEN/sendMessage failed')
    monkeypatch.setattr(main, 'send_telegram_notification', boom)
    main.send_notification('hxjob', 'HX Job', 'failed', 'x', 0)
    row = db.fetchone("SELECT last_error, last_error_at FROM notification_channels WHERE id = 'hxch'")
    assert row['last_error_at'] and 'SECRETTOKEN' not in row['last_error']
    [issue] = _issues('notification_failing')
    assert 'SECRETTOKEN' not in json.dumps(issue.to_dict())


def test_stale_lock_unlock_safety(world, db, monkeypatch, runner):
    """auto unlock only when old + managed host + no advisory lock + repo idle; held under the exclusive lock."""
    from app import health_service as hs
    from app.scheduler.locks import job_run_lock, repo_locks
    import app.main as main
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    lock = {'id': 'f' * 64, 'time': old, 'hostname': 'hx-web.lan', 'pid': 1234, 'username': 'root',
            'exclusive': False}
    present = {'locks': [dict(lock)]}
    monkeypatch.setattr(hs, 'list_repo_locks', lambda job, target, timeout=60: [dict(l) for l in present['locks']])
    unlocked = []

    class Proc:
        returncode, stdout, stderr = 0, 'successfully removed 1 locks', ''

    def fake_restic(job, target, args, timeout):
        if args[0] == 'unlock':
            unlocked.append(target['id'])
            present['locks'] = []
        return Proc()
    monkeypatch.setattr(hs, '_restic', fake_restic)
    monkeypatch.setattr(hs, 'probe_repo', lambda job, target, state: {})
    monkeypatch.setattr(hs, 'server_for_host', lambda state, host: None)
    state = hs.collect_state()
    url = state['job_storages']['hxjob'][0]['repo_url']
    hs.save_probe('repo:hxjob:hxs1', {'open': 'ok', 'locks': hs.decide_locks([dict(lock)], url, state), 'keys': []})
    [issue] = _issues('stale_lock')
    assert issue.fix == 'unlock_stale'

    # A backup of the job runs (job advisory lock held): the plan is refused
    with job_run_lock(main._lock_connection, 'hxjob') as got:
        assert got
        with pytest.raises(hs.NotApplicable):
            hs.FIXES['unlock_stale'].plan(issue)
    # The repository is in use (shared repo lock): the task can't get it exclusively -> busy, nothing unlocked
    row, _ = hs.enqueue_fix(issue, hs.FIXES['unlock_stale'])
    with repo_locks(main._lock_connection, [url]) as got:
        assert got
        assert runner.run_once() == 'failed'
    error = tasks.get_task(row['id'])['error']
    assert unlocked == [] and ('in use by another backupx run' in error or 'repository open' in error), error
    # Idle repository: unlocked
    row, _ = hs.enqueue_fix(issue, hs.FIXES['unlock_stale'])
    assert runner.run_once() == 'succeeded', tasks.get_task(row['id'])['error']
    assert unlocked == ['hxs1']


def test_foreign_or_fresh_locks_are_not_auto(world, db, monkeypatch):
    from app import health_service as hs
    state = hs.collect_state()
    url = state['job_storages']['hxjob'][0]['repo_url']
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    locks = hs.decide_locks([{'id': '1' * 64, 'time': old, 'hostname': 'someones-laptop'},
                             {'id': '2' * 64, 'time': fresh, 'hostname': 'hx-web.lan'}], url, state)
    assert [l['decision'] for l in locks] == ['confirm', 'wait']
    from app.scheduler.locks import job_run_lock
    import app.main as main
    with job_run_lock(main._lock_connection, 'hxjob:pitr'):
        locks = hs.decide_locks([{'id': '3' * 64, 'time': old, 'hostname': 'hx-web.lan'}], url, state)
    assert locks[0]['decision'] == 'wait' and 'PITR' in locks[0]['reason']


def test_recheck_and_dashboard_overview(api, db):
    r = api.post('/api/health/recheck', json={'resource_type': 'job', 'resource_id': 'hxjob'})
    assert r.status_code == 202 and r.get_json()['task']['kind'] == 'health_sweep'
    assert api.post('/api/health/recheck', json={'resource_type': 'job', 'resource_id': 'nope'}).status_code == 404
    assert api.post('/api/health/recheck', json={'resource_type': 'bogus', 'resource_id': 'x'}).status_code == 400
    _fail_storage(db, 'hxjob', 'hxs2')
    from app import health_service as hs
    hs.save_probe('stats:hxjob:hxs1', {'bytes': 1024, 'files': 3, 'snapshots': 2, 'storage_name': 'HX Primary',
                                       'job_name': 'HX Job'})
    body = api.get('/api/dashboard/overview').get_json()
    card = next(c for c in body['jobs'] if c['id'] == 'hxjob')
    assert [s['status'] for s in card['storages']] == [None, 'failed'] and card['failed_storage_ids'] == ['hxs2']
    assert card['issues']['critical'] + card['issues']['warning'] == 1
    assert card['retention']['state'] in ('off', 'never')
    assert any(u['job_id'] == 'hxjob' and u['bytes'] == 1024 for u in body['storage_usage'])
    assert isinstance(body['activity'], list)


def test_sweep_task_probes_server_and_repos(world, db, runner, monkeypatch):
    from app import health_service as hs
    import app.main as main
    calls = []
    monkeypatch.setattr(hs, 'probe_server', lambda s: calls.append(('ssh', s['id'])) or {'ok': True})
    monkeypatch.setattr(hs, 'probe_repo', lambda j, t, st: calls.append(('repo', j['id'], t['id'])) or {'open': 'error'})
    monkeypatch.setattr(hs, 'probe_prereqs', lambda jid, job: {})
    row, _ = tasks.enqueue('health_sweep', {'resource_type': 'job', 'resource_id': 'hxjob'})
    assert runner.run_once() == 'succeeded'
    assert sorted(calls) == [('repo', 'hxjob', 'hxs1'), ('repo', 'hxjob', 'hxs2'), ('ssh', 'hxsrv')]


def test_repo_probe_classifies_open_errors_and_redacts(world, db, monkeypatch):
    from app import health_service as hs

    class P:
        def __init__(self, rc, err=''):
            self.returncode, self.stdout, self.stderr = rc, '', err
    assert hs.classify_open(P(10, 'Fatal: repository does not exist'))[0] == 'not_initialized'
    assert hs.classify_open(P(12, 'Fatal: wrong password or no key found'))[0] == 'wrong_password'
    assert hs.classify_open(P(1, 'Fatal: unable to open config file: Stat: 403'))[0] == 'error'
    assert hs.classify_open(P(0))[0] == 'ok'
    import app.main as main
    job = dict(main.get_job('hxjob'), id='hxjob')
    target = main.get_job_storage_targets(job)[0]
    monkeypatch.setattr(hs, '_restic', lambda *a, **k: P(1, 'Fatal: s3 key SK-hxs1-secret rejected'))
    data = hs.probe_repo(job, target, hs.collect_state())
    assert data['open'] == 'error' and 'SK-hxs1-secret' not in data['open_error']
    stored = db.fetchone("SELECT data FROM health_probes WHERE key = 'repo:hxjob:hxs1'")['data']
    assert 'SK-hxs1-secret' not in stored


def test_ssh_probe_parsing_and_host_key_detection(world, db, monkeypatch):
    from app import health_service as hs
    import app.main as main
    data = hs.parse_ssh_probe('BXH_OK\nBXH_HOSTNAME web1\nBXH_RESTIC restic 0.17.3 compiled\n'
                              'BXH_DF / 91% 1048576\nBXH_DF / 91% 1048576\nBXH_DF /var 10% 5\n')
    assert data['ok'] and data['hostname'] == 'web1' and len(data['disks']) == 2

    class P:
        returncode, stdout = 255, ''
        stderr = ('@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n'
                  'Host key verification failed.\n')
    monkeypatch.setattr(main, '_ssh_run', lambda *a, **k: P())
    out = hs.probe_server(main.get_server('hxsrv'))
    assert out['ok'] is False and out['host_key_changed'] is True
    assert 'Ssh-Secret-pw1' not in json.dumps(out)


def test_backup_timeout_keeps_finished_storages_and_kills_silent_restic(monkeypatch):
    """A storage that hangs silently (restic retrying a 524 inside cat config) is cut by the watchdog;
    the storage that finished before keeps its success."""
    import subprocess
    import app.main as main
    monkeypatch.setattr(main, 'update_job_progress', lambda *a, **k: None)
    script = ('printf \'{"message_type":"backupx_target","index":0}\n\'; echo stored-ok; '
              'printf \'{"message_type":"backupx_target_done","index":0,"exit_code":0}\n\'; '
              'printf \'{"message_type":"backupx_target","index":1}\n\'; echo "Load(<config>) 524 retrying"; '
              'exec sleep 30')
    proc = subprocess.Popen(['sh', '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as e:
        main._stream_restic_progress(proc, 'hxjob', 2, ['primary', 'offsite'])
    assert time.monotonic() - started < 15
    preamble, results = main._timeout_results(e.value, [{'id': 'a'}, {'id': 'b'}])
    assert results[0]['exit_code'] == 0 and 'status' not in results[0]
    assert results[1]['status'] == 'timeout' and '524' in results[1]['output'][-1]


def test_timeout_results_are_recorded_per_storage(world, db):
    import app.main as main
    job = dict(main.get_job('hxjob'), id='hxjob')
    targets = main.get_job_storage_targets(job)
    run = main.BackupRun('hxjob', trigger='scheduled')
    with main._backup_run_context(run):
        main.record_run_results('hxjob', job, targets,
                                [{'exit_code': 0, 'output': ['ok']},
                                 {'exit_code': None, 'status': 'timeout', 'output': ['Load(<config>) 524 retrying']}],
                                overall_error='Backup timed out')
    rows = db.fetchall('SELECT storage_id, status, error FROM run_results WHERE run_id = %s ORDER BY id', (run.run_id,))
    assert [(r['storage_id'], r['status']) for r in rows] == [('hxs1', 'success'), ('hxs2', 'timeout')]
    assert '524' in rows[1]['error']
    [issue] = _issues('storage_unreachable')
    assert issue.fix == 'storage_retry' and issue.evidence['failed_storages'] == 'HX Offsite'


def test_unlock_over_ssh_keeps_secrets_out_of_argv(world, db, monkeypatch):
    from app import health_service as hs
    import app.main as main
    job = dict(main.get_job('hxjob'), id='hxjob')
    target = main.get_job_storage_targets(job)[0]
    calls = []

    class P:
        returncode, stdout, stderr = 0, 'successfully removed 1 locks', ''
    monkeypatch.setattr(main, '_ssh_run', lambda cmd, script, timeout: calls.append((list(cmd), script)) or P())

    class Ctx:
        def log(self, *a, **k):
            pass
    state = hs.collect_state()
    hs._restic_unlock(Ctx(), job, target, {'hostname': 'hx-web.lan', 'pid': 1}, state)
    [(argv, script)] = calls
    for secret in ('Restic-Secret-pw1', 'SK-hxs1-secret', 'AK-hxs1', 'Ssh-Secret-pw1'):
        assert secret not in ' '.join(argv)
    assert 'Restic-Secret-pw1' in script and 'restic unlock' in script and '--remove-all' not in script


# --- PITR streamer heartbeat between ticks ----------------------------------------------

def _pitr_state(heartbeat, last_tick_at, active='active', interval=5):
    state = base_state()
    state['jobs']['j1'].update(pitr_enabled=True, backup_type='database', pitr_interval=interval)
    state['pitr'] = {'j1': {'kind': 'mysql', 'state': 'ok', 'last_tick_at': last_tick_at,
                            'streamer_active': active, 'heartbeat': heartbeat}}
    return state


def test_heartbeat_read_at_last_tick_is_no_false_alarm_between_ticks():
    # Tick 5.5 min ago read a 40 s old heartbeat; the stored value is now 6+ min old: still fine
    state = _pitr_state(heartbeat=_ago(minutes=6, seconds=10), last_tick_at=_ago(minutes=5, seconds=30))
    assert health.detect(state) == []


def test_dead_streamer_is_seen_on_the_next_tick():
    # The streamer died 6 min ago; the tick 10 s ago read a heartbeat older than max_age (300 s)
    state = _pitr_state(heartbeat=_ago(minutes=6), last_tick_at=_ago(seconds=10))
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_streamer_down'
    assert issue.evidence['heartbeat_age'] == '6 min'
    # a stopped unit is down at once
    state = _pitr_state(heartbeat=_ago(seconds=30), last_tick_at=_ago(seconds=10), active='failed')
    assert kinds(health.detect(state)) == ['pitr_streamer_down']


def test_heartbeat_backstop_when_ticks_stop_landing():
    # Last tick 14 min ago saw a fresh heartbeat; nothing since: > max_age + interval + 2 min (12 min)
    state = _pitr_state(heartbeat=_ago(minutes=15), last_tick_at=_ago(minutes=14))
    assert kinds(health.detect(state)) == ['pitr_streamer_down']
    # but not yet after one missed tick
    state = _pitr_state(heartbeat=_ago(minutes=11), last_tick_at=_ago(minutes=10, seconds=30))
    assert health.detect(state) == []


def test_heartbeat_stalled_rules():
    assert health.heartbeat_stalled(None, _ago(seconds=5), NOW, 300) is False  # just started
    assert health.heartbeat_stalled(_ago(minutes=8), _ago(minutes=7, seconds=30), NOW, 300) is False
    assert health.heartbeat_stalled(_ago(minutes=8), _ago(minutes=2), NOW, 300) is True
    # a longer interval stretches only the backstop
    assert health.heartbeat_stalled(_ago(minutes=20), _ago(minutes=19), NOW, 300, interval_minutes=15) is False
    assert health.heartbeat_stalled(_ago(minutes=20), _ago(minutes=19), NOW, 300, interval_minutes=5) is True


# --- Dashboard activity: scheduled no-ops stay out --------------------------------------

def _finished_task(db, kind, result, created_by=None, status='succeeded', title=None):
    row, _ = tasks.enqueue(kind, {}, title=title or kind, created_by=created_by)
    db.execute('UPDATE tasks SET status = %s, result = %s, finished_at = %s WHERE id = %s',
               (status, json.dumps(result), tasks.utcnow(), row['id']))
    db.commit()
    return row['id']


def test_activity_hides_scheduled_noop_tasks(db):
    from app import health_service as hs
    noop = _finished_task(db, 'restore_cleanup', {'message': 'Nothing to clean up', 'noop': True,
                                                  'expired': []})
    legacy = _finished_task(db, 'restore_cleanup', {'message': 'Nothing to clean up', 'expired': []})
    did_work = _finished_task(db, 'restore_cleanup', {'message': '1 item(s) cleaned up', 'noop': False,
                                                      'expired': ['r1']})
    failed = _finished_task(db, 'restore_cleanup', {'message': 'x'}, status='failed')
    sweep = _finished_task(db, 'health_sweep', {'message': 'Checked 3 item(s): 0 issue(s), 0 critical'})
    manual_sweep = _finished_task(db, 'health_sweep', {'message': 'Checked 3'}, created_by='admin')
    by_person = _finished_task(db, 'restore_cleanup', {'message': 'Nothing to clean up', 'noop': True},
                               created_by='admin')
    shown = {e.get('task_id') for e in hs.activity() if e['type'] == 'task'}
    assert noop not in shown and legacy not in shown and sweep not in shown
    assert {did_work, failed, manual_sweep, by_person} <= shown
    # still in the task list
    ids = {t['id'] for t in tasks.list_tasks(limit=50)}
    assert noop in ids and legacy in ids


def test_restore_cleanup_marks_a_noop_result(monkeypatch):
    from app import restore_service as svc
    monkeypatch.setattr(svc, 'cleanup', lambda log=None: {'expired': [], 'interrupted': [], 'orphans': [],
                                                          'pre_restore_forgotten': []})

    class Ctx:
        def log(self, *a, **k):
            pass
    assert svc._task_cleanup(Ctx())['noop'] is True
    monkeypatch.setattr(svc, 'cleanup', lambda log=None: {'expired': ['r1'], 'interrupted': [], 'orphans': [],
                                                          'pre_restore_forgotten': []})
    out = svc._task_cleanup(Ctx())
    assert out['noop'] is False and out['message'] == '1 item(s) cleaned up'

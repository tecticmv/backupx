"""
Retention: job keep rules -> restic forget (after each backup) and a weekly prune.

restic itself is faked by replacing subprocess.run; the database writes and
notifications are captured. See tests/e2e notes in the PR for the run against a
real repository.
"""
import json
import subprocess
from contextlib import contextmanager

import pytest

from app import retention

RESTIC_PASSWORD = 'restic-Pa55-SECRET'
ACCESS_KEY = 'AKIA-ACCESS-SECRET'
SECRET_KEY = 's3-Secret-Key-SECRET'
SECRETS = [RESTIC_PASSWORD, ACCESS_KEY, SECRET_KEY]

DEFAULT_RULES = {'retention_hourly': 24, 'retention_daily': 7, 'retention_weekly': 4, 'retention_monthly': 12}


def _job(**extra):
    job = {'id': 'job1', 'name': 'Job 1', 'backup_prefix': 'job1', 'restic_password': RESTIC_PASSWORD,
           'backup_type': 'filesystem', **DEFAULT_RULES}
    job.update(extra)
    return job


def _target(name='primary', **extra):
    return {'id': name, 'name': name, 'endpoint': 's3.example.com', 'bucket': 'bucket',
            'access_key': ACCESS_KEY, 'secret_key': SECRET_KEY, 'skip_ssl_verify': False,
            'status': 'active', **extra}


def _snap(sid, host='web1', tags=('automated',)):
    return {'id': sid * 8, 'short_id': sid, 'hostname': host, 'tags': list(tags)}


def _group(keep, remove, host='web1', tags=('automated',)):
    return {'host': host, 'tags': list(tags), 'paths': ['/data'],
            'keep': [_snap(s, host, tags) for s in keep],
            'remove': [_snap(s, host, tags) for s in remove] if remove else None}


# ---------------------------------------------------------------------------
# Policy -> restic flags
# ---------------------------------------------------------------------------

def test_default_policy_maps_to_keep_flags():
    args = retention.forget_plan_args(_job())
    assert args[:3] == ['forget', '--json', '--dry-run']
    assert args[args.index('--tag') + 1] == 'automated'
    for flag, value in [('--keep-hourly', '24'), ('--keep-daily', '7'),
                        ('--keep-weekly', '4'), ('--keep-monthly', '12')]:
        assert args[args.index(flag) + 1] == value


def test_zero_and_invalid_rules_are_left_out():
    job = _job(retention_hourly=0, retention_daily='3', retention_weekly=-2, retention_monthly='abc')
    assert retention.retention_policy(job) == [('--keep-daily', 3)]
    args = retention.forget_plan_args(job)
    assert '--keep-hourly' not in args and '--keep-weekly' not in args and '--keep-monthly' not in args


@pytest.mark.parametrize('rules', [
    {'retention_hourly': 0, 'retention_daily': 0, 'retention_weekly': 0, 'retention_monthly': 0},
    {'retention_hourly': None, 'retention_daily': None, 'retention_weekly': None, 'retention_monthly': None},
    {'retention_hourly': '', 'retention_daily': -1, 'retention_weekly': 'x', 'retention_monthly': 0},
])
def test_no_rules_means_no_forget(rules):
    job = _job(**rules)
    assert retention.retention_policy(job) == []
    assert retention.forget_plan_args(job) is None
    assert retention.describe_policy(job) == 'none (keep everything)'


def test_missing_fields_mean_no_forget():
    job = {'id': 'j', 'name': 'j', 'restic_password': 'x'}
    assert retention.forget_plan_args(job) is None


def test_group_by_leaves_out_paths_for_database_dumps():
    # Dumps go to a fresh mktemp dir each run: grouping by paths would keep everything
    assert retention.group_by(_job(backup_type='database')) == 'host,tags'
    assert retention.group_by(_job(backup_type='filesystem')) == 'host,paths,tags'
    args = retention.forget_plan_args(_job(backup_type='database'))
    assert args[args.index('--group-by') + 1] == 'host,tags'


def test_prune_args():
    assert retention.prune_args(False, '10%') == ['prune', '--max-unused', '10%']
    assert retention.prune_args(True, '5%') == ['prune', '--max-unused', '5%', '--dry-run']


def test_settings_defaults(monkeypatch):
    for name in ('RETENTION_ENABLED', 'RETENTION_DRY_RUN', 'RETENTION_PRUNE_CRON', 'RETENTION_PRUNE_MAX_UNUSED'):
        monkeypatch.delenv(name, raising=False)
    cfg = retention.settings()
    assert cfg['enabled'] is False  # deploy is inert until turned on
    assert cfg['dry_run'] is False
    assert cfg['prune_cron'] == '0 4 * * sun'
    assert cfg['prune_max_unused'] == '10%'
    monkeypatch.setenv('RETENTION_ENABLED', 'true')
    monkeypatch.setenv('RETENTION_DRY_RUN', 'yes')
    cfg = retention.settings()
    assert cfg['enabled'] and cfg['dry_run']


# ---------------------------------------------------------------------------
# Plan checks
# ---------------------------------------------------------------------------

def test_plan_check_refuses_to_empty_a_group():
    remove, keep_count, problems = retention.check_forget_plan([
        _group(['a1'], ['b1']),
        _group([], ['c1', 'c2'], host='db1', tags=('automated', 'postgres-backup')),
    ])
    assert keep_count == 1
    assert len(problems) == 1 and 'db1' in problems[0] and 'postgres-backup' in problems[0]


def test_plan_check_collects_removals():
    remove, keep_count, problems = retention.check_forget_plan([_group(['a1', 'a2'], ['b1', 'b2']),
                                                                _group(['c1'], None, host='web2')])
    assert problems == []
    assert keep_count == 3
    assert [s['short_id'] for s in remove] == ['b1', 'b2']


def test_parse_prune_output():
    text = """loading indexes...
to repack:            12 blobs / 1.500 MiB
this removes:          4 blobs / 512.000 KiB
to delete:            30 blobs / 2.000 GiB
total prune:          34 blobs / 2.001 GiB
remaining:           100 blobs / 10.000 GiB
unused size after prune: 0 B (0.00% of remaining size)
done
"""
    parsed = retention.parse_prune_output(text)
    assert parsed['bytes_freed'] == int(2.001 * 1024 ** 3)
    assert 'total prune: 34 blobs / 2.001 GiB' in parsed['summary']
    assert retention.parse_prune_output('nothing here') == {'bytes_freed': None, 'summary': ''}


def test_parse_restic_version():
    v = retention.parse_restic_version('restic 0.18.1 compiled with go1.25.1 on linux/amd64')
    assert v == (0, 18, 1)
    assert retention.supports_retry_lock(v)
    assert not retention.supports_retry_lock(retention.parse_restic_version('restic 0.14.0 compiled with go1.19'))
    assert not retention.supports_retry_lock(None)


# ---------------------------------------------------------------------------
# Running forget / prune (restic faked)
# ---------------------------------------------------------------------------

class FakeRestic:
    """Stands in for subprocess.run; answers restic commands from a script."""

    def __init__(self, plan=None, version='restic 0.18.1 compiled with go1.25 on linux/amd64'):
        self.plan = plan or []
        self.version = version
        self.calls = []
        self.fail = {}  # subcommand -> (returncode, stderr)
        self.prune_stdout = 'total prune:          3 blobs / 1.000 MiB\n'

    def __call__(self, cmd, capture_output=False, text=False, env=None, timeout=None, **kw):
        self.calls.append({'cmd': list(cmd), 'env': env})
        sub = cmd[1]
        if sub == 'version':
            return subprocess.CompletedProcess(cmd, 0, self.version, '')
        if sub in self.fail:
            rc, err = self.fail[sub]
            return subprocess.CompletedProcess(cmd, rc, '', err)
        if sub == 'forget' and '--dry-run' in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.plan), '')
        if sub == 'prune':
            return subprocess.CompletedProcess(cmd, 0, self.prune_stdout, '')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    def restic_calls(self, sub=None):
        return [c for c in self.calls if c['cmd'][1] != 'version' and (sub is None or c['cmd'][1] == sub)]


@pytest.fixture
def env(monkeypatch):
    """Retention enabled, restic faked, DB writes and notifications captured."""
    import app.main as main
    monkeypatch.setenv('RETENTION_ENABLED', 'true')
    monkeypatch.delenv('RETENTION_DRY_RUN', raising=False)
    fake = FakeRestic()
    monkeypatch.setattr(main.subprocess, 'run', fake)
    monkeypatch.setattr(main, '_local_restic_version_cache', [])
    rec = {'runs': [], 'notify': [], 'others': {}}
    monkeypatch.setattr(main, 'record_retention_run',
                        lambda job_id, target, action, status, message, **kw:
                        rec['runs'].append({'action': action, 'status': status, 'message': message,
                                            'storage': target.get('name'), **kw}))
    monkeypatch.setattr(main, 'send_notification', lambda *a, **kw: rec['notify'].append(a))
    monkeypatch.setattr(main, 'invalidate_snapshot_cache', lambda job_id: None)
    monkeypatch.setattr(main, 'load_jobs', lambda: rec['others'])
    monkeypatch.setattr(main, 'get_job_storage_targets', lambda job, include_inactive=False: job.get('_targets', []))
    rec['fake'] = fake
    rec['main'] = main
    return rec


def _assert_no_secrets_in_argv(calls):
    for call in calls:
        text = '\0'.join(call['cmd'])
        for secret in SECRETS:
            assert secret not in text, f'secret in argv: {call["cmd"]}'


def test_disabled_does_nothing(env, monkeypatch):
    monkeypatch.setenv('RETENTION_ENABLED', 'false')
    env['main'].apply_retention('job1', _job(), [_target()])
    assert env['fake'].restic_calls() == []
    assert env['runs'] == []


def test_job_without_rules_never_forgets(env):
    job = _job(retention_hourly=0, retention_daily=0, retention_weekly=0, retention_monthly=0)
    env['main'].apply_retention('job1', job, [_target()])
    assert env['fake'].restic_calls() == []
    assert env['runs'] == []


def test_forget_removes_exactly_the_checked_snapshots(env):
    env['fake'].plan = [_group(['k1', 'k2'], ['r1', 'r2'])]
    env['main'].apply_retention('job1', _job(), [_target()])

    plan_call, forget_call = env['fake'].restic_calls('forget')
    assert '--dry-run' in plan_call['cmd']
    assert forget_call['cmd'][:4] == ['restic', 'forget', 'r1' * 8, 'r2' * 8]
    assert '--dry-run' not in forget_call['cmd']
    # Only snapshot IDs, no policy flags, on the call that removes
    assert not any(a.startswith('--keep') for a in forget_call['cmd'])
    assert '--retry-lock' in forget_call['cmd']
    assert env['runs'] == [{'action': 'forget', 'status': 'success', 'storage': 'primary',
                            'message': env['runs'][0]['message'], 'snapshots_removed': 2, 'dry_run': False}]
    assert 'removed 2 snapshot(s), kept 2' in env['runs'][0]['message']
    assert env['notify'] == []


def test_secrets_go_in_env_not_argv(env):
    env['fake'].plan = [_group(['k1'], ['r1'])]
    env['main'].apply_retention('job1', _job(), [_target()])
    calls = env['fake'].restic_calls()
    assert calls
    _assert_no_secrets_in_argv(calls)
    for call in calls:
        assert call['env']['RESTIC_PASSWORD'] == RESTIC_PASSWORD
        assert call['env']['AWS_ACCESS_KEY_ID'] == ACCESS_KEY
        assert call['env']['AWS_SECRET_ACCESS_KEY'] == SECRET_KEY
        assert call['env']['RESTIC_REPOSITORY'] == 's3:https://s3.example.com/bucket/job1'


def test_dry_run_only_plans(env, monkeypatch):
    monkeypatch.setenv('RETENTION_DRY_RUN', 'true')
    env['fake'].plan = [_group(['k1'], ['r1', 'r2', 'r3'])]
    env['main'].apply_retention('job1', _job(), [_target()])
    calls = env['fake'].restic_calls('forget')
    assert len(calls) == 1 and '--dry-run' in calls[0]['cmd']
    assert env['runs'][0]['dry_run'] is True
    assert 'would remove 3 snapshot(s)' in env['runs'][0]['message']


def test_refuses_when_a_group_would_be_emptied(env):
    env['fake'].plan = [_group(['k1'], ['r1']), _group([], ['x1'], host='web2')]
    env['main'].apply_retention('job1', _job(), [_target()])
    assert len(env['fake'].restic_calls('forget')) == 1  # plan only, nothing removed
    assert env['runs'][0]['status'] == 'failed'
    assert 'refused' in env['runs'][0]['message']
    assert len(env['notify']) == 1 and env['notify'][0][2] == 'failed'


def test_nothing_to_remove(env):
    env['fake'].plan = [_group(['k1', 'k2'], None)]
    env['main'].apply_retention('job1', _job(), [_target()])
    assert len(env['fake'].restic_calls('forget')) == 1
    assert env['runs'][0]['status'] == 'success'
    assert env['runs'][0]['snapshots_removed'] == 0


def test_forget_failure_notifies_and_continues_with_next_storage(env):
    fake = env['fake']
    fake.plan = [_group(['k1'], ['r1'])]
    original = fake.__call__

    def per_repo(cmd, **kw):
        if 'broken' in kw['env']['RESTIC_REPOSITORY']:
            fake.calls.append({'cmd': list(cmd), 'env': kw['env']})
            return subprocess.CompletedProcess(cmd, 1, '', 'Fatal: unable to open repository')
        return original(cmd, **kw)

    env['main'].subprocess.run = per_repo
    env['main'].apply_retention('job1', _job(), [_target('broken', endpoint='broken.example.com'),
                                                 _target('good')])
    statuses = [(r['storage'], r['status']) for r in env['runs']]
    assert statuses == [('broken', 'failed'), ('good', 'success')]
    assert len(env['notify']) == 1
    assert 'unable to open repository' in env['notify'][0][3]


def test_shared_repository_is_skipped(env):
    target = _target()
    env['others']['job2'] = {'name': 'Other job', 'backup_prefix': 'job1', '_targets': [target]}
    env['main'].apply_retention('job1', _job(), [target])
    assert env['fake'].restic_calls() == []
    assert env['runs'][0]['status'] == 'skipped'
    assert 'Other job' in env['runs'][0]['message']


def test_old_restic_gets_no_retry_lock(env):
    env['fake'].version = 'restic 0.14.0 compiled with go1.19'
    env['fake'].plan = [_group(['k1'], ['r1'])]
    env['main'].apply_retention('job1', _job(), [_target()])
    assert all('--retry-lock' not in c['cmd'] for c in env['fake'].restic_calls())


def test_insecure_tls_is_passed(env):
    env['fake'].plan = [_group(['k1'], None)]
    env['main'].apply_retention('job1', _job(), [_target(skip_ssl_verify=True)])
    assert '--insecure-tls' in env['fake'].restic_calls()[0]['cmd']


def test_finish_backup_forgets_only_on_storages_that_got_the_snapshot(env, monkeypatch):
    main = env['main']
    seen = []
    monkeypatch.setattr(main, 'apply_retention', lambda job_id, job, targets: seen.append([t['name'] for t in targets]))
    for name in ('update_job_progress', 'update_job_status', 'add_history'):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    targets = [_target('good'), _target('bad')]
    results = [{'exit_code': 0, 'output': []}, {'exit_code': 1, 'output': ['boom']}]
    main._finish_backup('job1', _job(), targets, main.utc_now(), 1, '', [], results, 'Backup')
    assert seen == [['good']]

    seen.clear()
    results = [{'exit_code': 0, 'output': []}, {'exit_code': 0, 'output': []}]
    main._finish_backup('job1', _job(), targets, main.utc_now(), 0, '', [], results, 'Backup')
    assert seen == [['good', 'bad']]

    seen.clear()
    results = [{'exit_code': 1, 'output': ['x']}, {'exit_code': 1, 'output': ['y']}]
    main._finish_backup('job1', _job(), targets, main.utc_now(), 1, '', [], results, 'Backup')
    assert seen == []


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------

@pytest.fixture
def lock(monkeypatch):
    import app.main as main
    state = {'acquired': True, 'taken': []}

    @contextmanager
    def fake_lock(connect, job_id):
        state['taken'].append(job_id)
        yield state['acquired']

    monkeypatch.setattr(main, 'job_run_lock', fake_lock)
    monkeypatch.setattr(main, '_lock_connection', lambda: None)
    return state


def test_prune_runs_under_the_job_lock(env, lock):
    main = env['main']
    main.prune_target('job1', _job(), _target(), main.retention.settings())
    assert lock['taken'] == ['job1']
    (call,) = env['fake'].restic_calls('prune')
    assert call['cmd'][:4] == ['restic', 'prune', '--max-unused', '10%']
    assert '--dry-run' not in call['cmd']
    assert '--retry-lock' in call['cmd']
    _assert_no_secrets_in_argv([call])
    assert env['runs'][0]['status'] == 'success'
    assert env['runs'][0]['bytes_freed'] == 1024 ** 2
    assert 'freed 1.0 MB' in env['runs'][0]['message'] or 'freed 1' in env['runs'][0]['message']


def test_prune_skips_while_a_backup_runs(env, lock):
    lock['acquired'] = False
    main = env['main']
    main.prune_target('job1', _job(), _target(), main.retention.settings())
    assert env['fake'].restic_calls() == []
    assert env['runs'][0]['status'] == 'skipped'
    assert env['notify'] == []


def test_prune_dry_run_and_max_unused(env, lock, monkeypatch):
    monkeypatch.setenv('RETENTION_DRY_RUN', '1')
    monkeypatch.setenv('RETENTION_PRUNE_MAX_UNUSED', '5%')
    main = env['main']
    main.prune_target('job1', _job(), _target(), main.retention.settings())
    (call,) = env['fake'].restic_calls('prune')
    assert call['cmd'][:5] == ['restic', 'prune', '--max-unused', '5%', '--dry-run']
    assert 'would free' in env['runs'][0]['message']


def test_prune_failure_notifies(env, lock):
    env['fake'].fail['prune'] = (1, 'Fatal: repository is already locked exclusively')
    main = env['main']
    main.prune_target('job1', _job(), _target(), main.retention.settings())
    assert env['runs'][0]['status'] == 'failed'
    assert len(env['notify']) == 1 and 'already locked' in env['notify'][0][3]


def test_maintenance_prunes_only_jobs_with_rules(env, lock, monkeypatch):
    main = env['main']
    monkeypatch.setattr(main, '_short_lived_db', contextmanager(lambda: (yield)))

    class _DB:
        def close(self):
            pass

    monkeypatch.setattr(main, 'get_db', lambda: _DB())
    env['others'].update({
        'a': {'name': 'A', 'backup_prefix': 'a', 'restic_password': 'p', **DEFAULT_RULES,
              '_targets': [_target('s1'), _target('s2', endpoint='r2.example.com')]},
        'b': {'name': 'B', 'backup_prefix': 'b', 'restic_password': 'p', 'retention_hourly': 0,
              'retention_daily': 0, 'retention_weekly': 0, 'retention_monthly': 0, '_targets': [_target('s1')]},
    })
    main.run_retention_maintenance()
    assert lock['taken'] == ['a', 'a']
    repos = [c['env']['RESTIC_REPOSITORY'] for c in env['fake'].restic_calls('prune')]
    assert repos == ['s3:https://s3.example.com/bucket/a', 's3:https://r2.example.com/bucket/a']


def test_maintenance_disabled_does_nothing(env, lock, monkeypatch):
    monkeypatch.setenv('RETENTION_ENABLED', 'false')
    main = env['main']
    monkeypatch.setattr(main, '_short_lived_db', contextmanager(lambda: (yield)))
    main.run_retention_maintenance()
    assert env['fake'].restic_calls() == []


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

@pytest.fixture
def sched(monkeypatch):
    import app.main as main
    new = main._new_scheduler('UTC')
    monkeypatch.setattr(main, 'scheduler', new)
    monkeypatch.setattr(main, '_scheduled_prune', {'cron': None})
    yield main, new
    if new.running:
        new.shutdown(wait=False)


def test_prune_not_scheduled_when_disabled(sched, monkeypatch):
    main, s = sched
    monkeypatch.delenv('RETENTION_ENABLED', raising=False)
    main._sync_prune_schedule()
    assert s.get_job(retention.PRUNE_SCHEDULE_ID) is None


def test_prune_scheduled_sunday_0400_on_its_own_executor(sched, monkeypatch):
    main, s = sched
    monkeypatch.setenv('RETENTION_ENABLED', 'true')
    monkeypatch.delenv('RETENTION_PRUNE_CRON', raising=False)
    s.start(paused=True)
    main._sync_prune_schedule()
    job = s.get_job(retention.PRUNE_SCHEDULE_ID)
    assert job is not None
    assert job.executor == 'maintenance'
    assert job.func is main.run_retention_maintenance
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields['hour'] == '4' and fields['minute'] == '0' and fields['day_of_week'] == 'sun'
    from datetime import datetime
    import pytz
    nxt = job.trigger.get_next_fire_time(None, datetime(2026, 10, 29, 12, 0, tzinfo=pytz.utc))
    assert nxt.strftime('%a %H:%M') == 'Sun 04:00'

    # Turned off again -> removed
    monkeypatch.setenv('RETENTION_ENABLED', 'false')
    main._sync_prune_schedule()
    assert s.get_job(retention.PRUNE_SCHEDULE_ID) is None


def test_custom_and_invalid_prune_cron(sched, monkeypatch):
    main, s = sched
    monkeypatch.setenv('RETENTION_ENABLED', 'true')
    s.start(paused=True)
    monkeypatch.setenv('RETENTION_PRUNE_CRON', '30 5 * * sat')
    main._sync_prune_schedule()
    fields = {f.name: str(f) for f in s.get_job(retention.PRUNE_SCHEDULE_ID).trigger.fields}
    assert fields['hour'] == '5' and fields['day_of_week'] == 'sat'

    monkeypatch.setenv('RETENTION_PRUNE_CRON', 'every sunday')
    main._sync_prune_schedule()
    assert s.get_job(retention.PRUNE_SCHEDULE_ID) is None


def test_scheduler_has_separate_maintenance_executor():
    import app.main as main
    s = main._new_scheduler('UTC')
    assert 'maintenance' in s._executors
    assert s._executors['maintenance']._pool._max_workers == 1
    assert s._executors['default']._pool._max_workers == main.BACKUP_MAX_CONCURRENT


def test_prune_scheduler_error_alerts_without_failing_a_job(monkeypatch):
    import app.main as main
    from apscheduler.events import JobExecutionEvent, EVENT_JOB_ERROR
    reported, notified = [], []
    monkeypatch.setattr(main, '_report_backup_failure', lambda *a, **kw: reported.append(a))
    monkeypatch.setattr(main, 'send_notification', lambda *a, **kw: notified.append(a))

    class _T:
        def __init__(self, target, args, **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(main.threading, 'Thread', _T)
    event = JobExecutionEvent(EVENT_JOB_ERROR, retention.PRUNE_SCHEDULE_ID, 'default', None,
                              exception=RuntimeError('boom'))
    main._on_scheduler_event(event)
    assert reported == []
    assert notified and notified[0][2] == 'failed'


def test_retention_runs_table_in_schema():
    import inspect
    from app.db.postgres import PostgresBackend
    assert 'CREATE TABLE IF NOT EXISTS retention_runs' in inspect.getsource(PostgresBackend.init_schema)
    assert 'CREATE TABLE IF NOT EXISTS retention_runs' in inspect.getsource(PostgresBackend.migrate_schema)

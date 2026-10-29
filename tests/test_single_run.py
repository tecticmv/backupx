"""
Tests that scheduled backups run once: one scheduler across workers, one run per
job at a time, and restic init only for repositories that don't exist yet.
"""
import os
import shutil
import subprocess

import pytest

from tests.test_multi_storage import JOB, _target, recorded  # noqa: F401 (fixture)


# ---------------------------------------------------------------------------
# restic init only when the repository is missing
# ---------------------------------------------------------------------------

def _run_target_script(main, fake_restic, targets):
    script = (fake_restic
              + main._restic_target_blocks(JOB, targets, 'restic backup --json /data')
              + 'exit $BACKUPX_FAILED\n')
    proc = subprocess.Popen([shutil.which('bash'), '-c', script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return main._stream_restic_progress(proc, 'job1', 30, [t['name'] for t in targets])


# Fake restic: "cat config" behaves per repository, everything else succeeds and
# is logged so the test can see whether init/backup ran.
FAKE_RESTIC = r'''
restic() {
  echo "CALLED $1" >&2
  if [ "$1" = cat ]; then
    case "$RESTIC_REPOSITORY" in
      *missing17*) echo "Fatal: repository does not exist: unable to open config file" >&2; return 10;;
      *missing14*) printf 'Fatal: unable to open config file: Stat: The specified key does not exist.\nIs there a repository at the following location?\ns3:x\n' >&2; return 1;;
      *denied14*) printf 'Fatal: unable to open config file: Stat: Access Denied.\nIs there a repository at the following location?\ns3:x\n' >&2; return 1;;
      *damaged*) echo "Fatal: config or key a86a866c is damaged: ciphertext verification failed" >&2; return 1;;
    esac
    return 0
  fi
  echo "RAN $1"
}
'''


@pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')
@pytest.mark.parametrize('name, expect_init', [
    ('missing17', True),    # restic >= 0.17: exit code 10
    ('missing14', True),    # older restic: message only
    ('existing', False),
])
def test_init_only_when_repository_missing(recorded, name, expect_init):
    import app.main as main

    returncode, _, _, results = _run_target_script(main, FAKE_RESTIC, [_target(name, f'{name}.example.com')])

    assert returncode == 0
    assert results[0]['exit_code'] == 0
    assert ('RAN init' in results[0]['output']) == expect_init
    assert 'RAN backup' in results[0]['output']


@pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')
@pytest.mark.parametrize('name, error', [
    ('damaged', 'ciphertext verification failed'),
    ('denied14', 'Access Denied'),
])
def test_open_failure_fails_storage_without_init(recorded, name, error):
    import app.main as main

    targets = [_target(name, f'{name}.example.com'), _target('existing', 'existing.example.com')]
    returncode, _, _, results = _run_target_script(main, FAKE_RESTIC, targets)

    assert returncode == 1
    assert results[0]['exit_code'] == 1
    assert 'RAN init' not in results[0]['output']
    assert 'RAN backup' not in results[0]['output']
    assert any(error in line for line in results[0]['output'])
    assert error in main._target_error(results[0])
    # The other storage is unaffected
    assert results[1]['exit_code'] == 0


@pytest.mark.skipif(shutil.which('bash') is None or shutil.which('restic') is None,
                    reason='needs bash and restic')
def test_real_restic_inits_missing_repo_but_not_wrong_password(recorded, monkeypatch, tmp_path):
    import app.main as main

    repo = tmp_path / 'repo'
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'file.txt').write_text('hello')
    monkeypatch.setattr(main, 'job_repo_url', lambda job, target: str(repo))
    targets = [_target('local', 'unused')]
    backup_cmd = f'restic backup --json {data}'

    def run(password):
        script = (f'export RESTIC_PASSWORD={password}\n'
                  + main._restic_target_blocks(JOB, targets, backup_cmd)
                  + 'exit $BACKUPX_FAILED\n')
        proc = subprocess.Popen([shutil.which('bash'), '-c', script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return main._stream_restic_progress(proc, 'job1', 60, ['local'])

    returncode, _, _, results = run('right')
    assert returncode == 0, results
    assert 'Initializing new restic repository...' in results[0]['output']

    returncode, _, _, results = run('right')
    assert returncode == 0, results
    assert 'Initializing new restic repository...' not in results[0]['output']

    keys_before = sorted(os.listdir(repo / 'keys'))
    returncode, _, _, results = run('wrong')
    assert returncode == 1
    assert 'Initializing new restic repository...' not in results[0]['output']
    assert 'wrong password' in main._target_error(results[0])
    assert sorted(os.listdir(repo / 'keys')) == keys_before


# ---------------------------------------------------------------------------
# Advisory locks (need a reachable Postgres; skipped otherwise)
# ---------------------------------------------------------------------------

@pytest.fixture
def connect():
    if not os.environ.get('DATABASE_USER'):
        pytest.skip('needs DATABASE_* settings for a test Postgres')
    from app.db.factory import get_database
    db = get_database()
    try:
        db.open_dedicated_connection().close()
    except Exception as e:
        pytest.skip(f'Postgres not reachable: {e}')
    return db.open_dedicated_connection


def _leader(connect, events, name):
    from app.scheduler.locks import SchedulerLeader
    return SchedulerLeader(connect,
                           on_acquire=lambda: events.append(f'{name} acquire'),
                           on_lose=lambda: events.append(f'{name} lose'),
                           on_tick=lambda: events.append(f'{name} tick'),
                           key=4242)  # not the app's own scheduler lock, which this process may hold


def test_only_one_scheduler_leader_and_failover(connect):
    events = []
    a, b = _leader(connect, events, 'a'), _leader(connect, events, 'b')
    try:
        a.check()
        b.check()
        assert a.is_leader and not b.is_leader
        assert events == ['a acquire']

        a.check()
        assert events[-1] == 'a tick'

        # Leader's database session dies (worker killed): the lock is released
        a._conn.close()
        b.check()
        assert b.is_leader

        a.check()
        assert not a.is_leader
        assert 'a lose' in events
        a.check()
        assert not a.is_leader
    finally:
        a.stop()
        b.stop()


def test_job_run_lock_is_exclusive(connect):
    from app.scheduler.locks import job_run_lock, job_is_running

    assert not job_is_running(connect, 'job-lock-test')
    with job_run_lock(connect, 'job-lock-test') as first:
        assert first
        with job_run_lock(connect, 'job-lock-test') as second:
            assert not second
        assert job_is_running(connect, 'job-lock-test')
        with job_run_lock(connect, 'other-job') as other:
            assert other
    assert not job_is_running(connect, 'job-lock-test')


def test_concurrent_run_is_skipped_quietly(connect, monkeypatch):
    import app.main as main
    from app.scheduler.locks import job_run_lock

    calls = []
    monkeypatch.setattr(main, '_lock_connection', connect)
    monkeypatch.setattr(main, '_run_backup_inner', lambda job_id: calls.append(job_id) or (True, 'ok'))
    monkeypatch.setattr(main, 'send_notification', lambda *a, **kw: calls.append('notified'))

    with job_run_lock(connect, 'job-skip-test') as held:
        assert held
        ok, message = main.run_backup('job-skip-test')
    assert not ok
    assert 'already running' in message
    assert calls == []

    assert main.run_backup('job-skip-test') == (True, 'ok')
    assert calls == ['job-skip-test']

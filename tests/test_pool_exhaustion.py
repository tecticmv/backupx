"""
Tests for the 02:00 pile-up: many jobs firing at once must queue, borrow DB
connections briefly, and never fail without a trace.
"""
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

psycopg2 = pytest.importorskip('psycopg2')
from psycopg2 import pool as pg_pool  # noqa: E402


# ---------------------------------------------------------------------------
# Pool waits for a free connection instead of raising
# ---------------------------------------------------------------------------

class _FakeConn:
    closed = 0

    def __init__(self):
        self.info = SimpleNamespace(transaction_status=psycopg2.extensions.TRANSACTION_STATUS_IDLE)

    def rollback(self):
        pass

    def close(self):
        self.closed = 1


@pytest.fixture
def fake_pool(monkeypatch):
    from app.db.postgres import BlockingThreadedConnectionPool
    monkeypatch.setattr(pg_pool.psycopg2, 'connect', lambda *a, **kw: _FakeConn())
    return lambda maxconn, timeout: BlockingThreadedConnectionPool(0, maxconn, timeout=timeout)


def test_pool_waits_for_a_returned_connection(fake_pool):
    p = fake_pool(2, timeout=5)
    a, b = p.getconn(), p.getconn()
    got = []
    waiter = threading.Thread(target=lambda: got.append(p.getconn()))
    waiter.start()
    time.sleep(0.3)
    assert not got, 'third checkout should wait while the pool is full'
    p.putconn(a)
    waiter.join(2)
    assert len(got) == 1
    p.putconn(b)
    p.putconn(got[0])


def test_pool_times_out_with_pool_error(fake_pool):
    p = fake_pool(1, timeout=0.2)
    conn = p.getconn()
    start = time.monotonic()
    with pytest.raises(pg_pool.PoolError):
        p.getconn()
    assert time.monotonic() - start >= 0.2
    p.putconn(conn)
    p.putconn(p.getconn())  # the slot is usable again


def test_failed_connect_frees_the_slot(fake_pool, monkeypatch):
    p = fake_pool(1, timeout=0.2)

    def boom(*a, **kw):
        raise psycopg2.OperationalError('db down')
    monkeypatch.setattr(pg_pool.psycopg2, 'connect', boom)
    with pytest.raises(psycopg2.OperationalError):
        p.getconn()
    monkeypatch.setattr(pg_pool.psycopg2, 'connect', lambda *a, **kw: _FakeConn())
    p.putconn(p.getconn())


# ---------------------------------------------------------------------------
# Short-lived checkouts (need a reachable Postgres; skipped otherwise)
# ---------------------------------------------------------------------------

@pytest.fixture
def pg_backend():
    if not os.environ.get('DATABASE_USER'):
        pytest.skip('needs DATABASE_* settings for a test Postgres')
    from app.db.postgres import PostgresBackend
    try:
        db = PostgresBackend(host=os.environ.get('DATABASE_HOST', 'localhost'),
                             port=int(os.environ.get('DATABASE_PORT', 5432)),
                             database=os.environ.get('DATABASE_NAME', 'backupx'),
                             user=os.environ['DATABASE_USER'],
                             password=os.environ.get('DATABASE_PASSWORD', ''),
                             pool_min=0, pool_max=2, pool_timeout=2)
    except Exception as e:
        pytest.skip(f'Postgres not reachable: {e}')
    db.execute('CREATE TEMP TABLE IF NOT EXISTS t_pool (v int)')  # warm-up
    db.commit()
    db.close()
    yield db
    db.close_all()


def _held(db):
    return getattr(db._local, 'connection', None) is not None


def test_short_lived_mode_returns_connection_after_each_operation(pg_backend):
    db = pg_backend
    with db.short_lived_connections():
        assert db.fetchone('SELECT 1 AS one')['one'] == 1
        assert not _held(db)
        db.fetchall('SELECT 1')
        assert not _held(db)

        # A write stays on its connection until committed
        db.execute('CREATE TABLE IF NOT EXISTS pool_test (v int)')
        db.execute('INSERT INTO pool_test VALUES (1)')
        assert _held(db)
        db.fetchone('SELECT count(*) AS n FROM pool_test')  # read inside the open transaction
        assert _held(db)
        db.commit()
        assert not _held(db)
        assert db.fetchone('SELECT count(*) AS n FROM pool_test')['n'] >= 1
        db.execute('DROP TABLE pool_test')
        db.commit()
    assert not _held(db)


def test_default_mode_keeps_thread_connection(pg_backend):
    db = pg_backend
    db.fetchone('SELECT 1')
    assert _held(db)
    db.close()
    assert not _held(db)


def test_many_short_lived_threads_share_a_small_pool(pg_backend):
    """10 'backup runs' on a 2-connection pool: none fail, because no run holds a connection"""
    db = pg_backend
    errors = []

    def run():
        try:
            with db.short_lived_connections():
                for _ in range(5):
                    db.fetchone('SELECT pg_sleep(0.02)')
                    time.sleep(0.05)  # the "backup work" between queries
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []


# ---------------------------------------------------------------------------
# A run never fails silently
# ---------------------------------------------------------------------------

@pytest.fixture
def reported(monkeypatch):
    import app.main as main
    calls = {'status': [], 'progress': [], 'history': [], 'notify': []}

    @contextmanager
    def free_lock(connect, job_id):
        yield True

    monkeypatch.setattr(main, 'job_run_lock', free_lock)
    monkeypatch.setattr(main, 'update_job_status', lambda job_id, s, **kw: calls['status'].append((job_id, s)))
    monkeypatch.setattr(main, 'update_job_progress', lambda job_id, p, m: calls['progress'].append((job_id, m)))
    monkeypatch.setattr(main, 'add_history',
                        lambda job_id, name, s, m, d=0: calls['history'].append((job_id, name, s, m)))
    monkeypatch.setattr(main, 'send_notification',
                        lambda job_id, name, s, m, d: calls['notify'].append((job_id, name, s, m)))
    return calls


def test_get_job_failure_is_reported(reported, monkeypatch):
    import app.main as main

    def exhausted(job_id):
        raise pg_pool.PoolError('connection pool exhausted')
    monkeypatch.setattr(main, 'get_job', exhausted)

    ok, message = main.run_backup('job-x')

    assert not ok
    assert 'PoolError' in message and 'exhausted' in message
    assert reported['status'] == [('job-x', 'failed')]
    assert reported['progress'] == [('job-x', message)]
    assert reported['history'] == [('job-x', 'job-x', 'failed', message)]
    assert reported['notify'] == [('job-x', 'job-x', 'failed', message)]


def test_crash_mid_run_uses_job_name(reported, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'get_job', lambda job_id: {'id': job_id, 'name': 'Website DB'})

    def crash(job_id):
        raise RuntimeError('ssh exploded')
    monkeypatch.setattr(main, '_run_backup_inner', crash)

    ok, message = main.run_backup('job-y')
    assert not ok
    assert reported['history'][0][1] == 'Website DB'
    assert reported['notify'][0][1:3] == ('Website DB', 'failed')


def test_lock_connection_failure_is_reported(monkeypatch):
    """Real job_run_lock, but its connection can't be opened"""
    import app.main as main

    calls = {'history': [], 'notify': []}
    monkeypatch.setattr(main, 'get_job', lambda job_id: {'id': job_id, 'name': 'Snipe-IT DB'})
    monkeypatch.setattr(main, 'update_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'update_job_progress', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'add_history', lambda job_id, name, s, m, d=0: calls['history'].append(name))
    monkeypatch.setattr(main, 'send_notification', lambda job_id, name, *a: calls['notify'].append(name))

    def refused():
        raise psycopg2.OperationalError('too many connections')
    monkeypatch.setattr(main, '_lock_connection', refused)

    ok, message = main.run_backup('job-z')
    assert not ok and 'too many connections' in message
    assert calls == {'history': ['Snipe-IT DB'], 'notify': ['Snipe-IT DB']}


def test_reporting_survives_database_outage(monkeypatch):
    """Status/history writes fail too: the notification still goes out"""
    import app.main as main

    def down(*a, **kw):
        raise psycopg2.OperationalError('db down')
    notified = []
    monkeypatch.setattr(main, 'get_job', down)
    monkeypatch.setattr(main, 'update_job_status', down)
    monkeypatch.setattr(main, 'update_job_progress', down)
    monkeypatch.setattr(main, 'add_history', down)
    monkeypatch.setattr(main, 'send_notification', lambda *a: notified.append(a))

    main._report_backup_failure('job-q', 'Backup failed: boom')  # must not raise
    assert notified == [('job-q', 'job-q', 'failed', 'Backup failed: boom', 0)]


def test_send_notification_falls_back_to_cached_channels(monkeypatch):
    import app.main as main
    channel = {'id': 'c', 'name': 'tg', 'type': 'telegram', 'enabled': True,
               'notify_on_success': True, 'notify_on_failure': True, 'config': {}}
    sent = []
    monkeypatch.setattr(main, 'send_telegram_notification', lambda ch, name, *a: sent.append(name))
    monkeypatch.setattr(main, 'load_notification_channels', lambda: [channel])
    main.send_notification('j', 'Job', 'failed', 'm', 0)

    def down():
        raise psycopg2.OperationalError('db down')
    monkeypatch.setattr(main, 'load_notification_channels', down)
    main.send_notification('j', 'Job', 'failed', 'm', 0)
    assert sent == ['Job', 'Job']


def test_scheduler_listener_reports_missed_runs(monkeypatch):
    import app.main as main
    from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

    done = threading.Event()
    reports = []
    monkeypatch.setattr(main, '_report_backup_failure',
                        lambda job_id, message: (reports.append((job_id, message)), done.set()))
    main._on_scheduler_event(JobExecutionEvent(EVENT_JOB_MISSED, 'job-m', 'default',
                                               datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)))
    assert done.wait(2)
    assert reports[0][0] == 'job-m' and 'did not start' in reports[0][1]


# ---------------------------------------------------------------------------
# Concurrency cap: a 02:00 pile-up queues instead of stampeding or being dropped
# ---------------------------------------------------------------------------

def test_scheduler_caps_concurrency_and_runs_every_queued_job():
    import app.main as main

    state = {'running': 0, 'peak': 0, 'done': []}
    lock = threading.Lock()

    def fake_backup(job_id):
        with lock:
            state['running'] += 1
            state['peak'] = max(state['peak'], state['running'])
        time.sleep(0.6)
        with lock:
            state['running'] -= 1
            state['done'].append(job_id)

    sched = main._new_scheduler('UTC')
    sched.start()
    try:
        fire = datetime.now(timezone.utc) + timedelta(seconds=0.5)
        for i in range(14):
            sched.add_job(fake_backup, 'date', run_date=fire, args=[f'job{i}'], id=f'job{i}')
        deadline = time.monotonic() + 15
        while len(state['done']) < 14 and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        sched.shutdown(wait=True)

    # Last batch waited ~1.8s for a slot: dropped as "missed" under APScheduler's default 1s grace
    assert sorted(state['done']) == sorted(f'job{i}' for i in range(14))
    assert state['peak'] == main.BACKUP_MAX_CONCURRENT == 4


# ---------------------------------------------------------------------------
# Overdue flag
# ---------------------------------------------------------------------------

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)


def _job(**kw):
    job = {'name': 'j', 'schedule_enabled': True, 'schedule_cron': '0 2 * * *', 'status': 'success',
           'created_at': datetime(2026, 1, 1), 'last_run': None}
    job.update(kw)
    return job


def _overdue(job, now=NOW, tz='UTC', grace=6 * 3600):
    from app.main import job_overdue_since
    return job_overdue_since(job, tz, now=now, grace=grace)


def test_overdue_when_last_run_before_expected_fire():
    # Last ran 3 weeks ago (the bug): due at 02:00 today
    assert _overdue(_job(last_run=datetime(2026, 10, 8, 2, 0))) == datetime(2026, 10, 29, 2, 0, tzinfo=UTC)


def test_not_overdue_when_ran_today():
    assert _overdue(_job(last_run='2026-10-29T02:00:04+00:00')) is None
    assert _overdue(_job(last_run=datetime(2026, 10, 29, 2, 31))) is None


def test_grace_covers_queued_runs():
    # 03:00: today's 02:00 run may still be queued; only yesterday's fire counts
    assert _overdue(_job(last_run=datetime(2026, 10, 28, 2, 0)),
                    now=datetime(2026, 10, 29, 3, 0, tzinfo=UTC)) is None


def test_overdue_ignores_unscheduled_running_and_new_jobs():
    assert _overdue(_job(schedule_enabled=False)) is None
    assert _overdue(_job(status='running')) is None
    assert _overdue(_job(created_at='2026-10-29T05:00:00+00:00')) is None
    assert _overdue(_job(schedule_cron='bad')) is None


def test_overdue_never_ran():
    assert _overdue(_job(last_run=None)) is not None


def test_overdue_uses_scheduler_timezone():
    # 02:00 in Maldives (UTC+5) = 21:00 UTC the previous day
    due = _overdue(_job(last_run=datetime(2026, 10, 20)), tz='Indian/Maldives')
    assert due == datetime(2026, 10, 28, 21, 0, tzinfo=UTC)


def test_overdue_rare_crons():
    # Monthly on the 1st: last ran in August -> due 1 Oct
    due = _overdue(_job(schedule_cron='0 2 1 * *', last_run=datetime(2026, 8, 1, 2, 0)))
    assert due == datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    assert _overdue(_job(schedule_cron='0 2 1 * *', last_run=datetime(2026, 10, 1, 2, 0))) is None
    # Weekly: APScheduler day names, as the scheduler uses (2026-10-26 is a Monday)
    due = _overdue(_job(schedule_cron='0 2 * * mon', last_run=datetime(2026, 10, 19, 2, 0)))
    assert due == datetime(2026, 10, 26, 2, 0, tzinfo=UTC)

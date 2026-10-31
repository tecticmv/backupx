"""
Schema init/migrations run under one Postgres advisory lock.

Every gunicorn worker imports the app and runs init_database(). On the
2026-10-31 deploy two workers raced on CREATE TABLE IF NOT EXISTS pitr_state
("already exists" on the table's row type) and the gunicorn master exited.
"""
import multiprocessing
import os
import uuid
from contextlib import contextmanager

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-32chars!')

from app.db import factory  # noqa: E402


class FakeDB:
    def __init__(self):
        self.calls = []
        self.locked = False

    @contextmanager
    def schema_lock(self):
        self.calls.append('lock')
        self.locked = True
        try:
            yield
        finally:
            self.locked = False
            self.calls.append('unlock')

    def init_schema(self):
        self.calls.append(('init', self.locked))

    def migrate_schema(self):
        self.calls.append(('migrate', self.locked))


def test_init_database_runs_schema_and_migrations_under_the_lock(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(factory, 'get_database', lambda: db)
    assert factory.init_database() is db
    assert db.calls == ['lock', ('init', True), ('migrate', True), 'unlock']


# ---------------------------------------------------------------------------
# Real Postgres: several processes initializing an empty database at once
# ---------------------------------------------------------------------------

def _pg_params(database):
    return dict(host=os.environ.get('DATABASE_HOST', 'localhost'),
                port=int(os.environ.get('DATABASE_PORT', 5432)),
                user=os.environ.get('DATABASE_USER'),
                password=os.environ.get('DATABASE_PASSWORD', ''),
                dbname=database)


def _admin_connection():
    psycopg2 = pytest.importorskip('psycopg2')
    if not os.environ.get('DATABASE_USER'):
        pytest.skip('needs a throwaway Postgres (DATABASE_HOST/USER/PASSWORD)')
    try:
        conn = psycopg2.connect(connect_timeout=5, **_pg_params('postgres'))
    except psycopg2.Error as exc:
        pytest.skip(f'Postgres not reachable: {exc}')
    conn.autocommit = True
    return conn


def _worker(database, start, results):
    os.environ['DATABASE_NAME'] = database
    os.environ['DATABASE_POOL_MIN'] = '1'
    os.environ['DATABASE_POOL_MAX'] = '2'
    from app.db import factory as f
    f._db_instance = None
    try:
        start.wait(30)
        f.init_database()
        f.close_database()
        results.put('ok')
    except Exception as exc:  # report, don't hang the parent
        results.put(f'{type(exc).__name__}: {exc}')


@pytest.mark.parametrize('round_no', range(5))
def test_concurrent_init_database_on_empty_postgres(round_no):
    admin = _admin_connection()
    database = f'bx_schema_lock_{uuid.uuid4().hex[:10]}'
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE {database}')
    try:
        ctx = multiprocessing.get_context('spawn')
        start, results = ctx.Event(), ctx.Queue()
        procs = [ctx.Process(target=_worker, args=(database, start, results)) for _ in range(4)]
        for p in procs:
            p.start()
        start.set()
        outcomes = [results.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(30)
        assert outcomes == ['ok'] * len(procs), outcomes
        assert all(p.exitcode == 0 for p in procs)
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS {database} WITH (FORCE)')
        admin.close()

"""Restore drills: comparison rules, read-only checksums, the drill task with faked restore/SQL."""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app import drill as d, restore as r, tasks


def test_compare_judges_only_quiet_tables():
    before = {'app.a': '1', 'app.b': '2', 'app.c': '3'}
    after = {'app.a': '1', 'app.b': '9', 'app.c': '3'}
    ok = d.compare(before, after, {'app.a': '1', 'app.b': 'x', 'app.c': '3'})
    assert ok['ok'] and ok['compared'] == 2 and ok['changed'] == ['app.b']
    bad = d.compare(before, after, {'app.a': '1', 'app.c': '4'})
    assert not bad['ok'] and bad['mismatches'][0]['table'] == 'app.c'
    missing = d.compare(before, after, {'app.a': '1'})
    assert not missing['ok'] and missing['missing'] == ['app.c']
    err = d.compare({'app.a': None}, {'app.a': None}, {})
    assert not err['ok'] and err['errors'] == ['app.a']


def test_checksum_statements_are_read_only():
    for engine in (r.ENGINE_MYSQL, r.ENGINE_MARIADB):
        for big in (False, True):
            assert d.is_read_only(d.checksum_statement(engine, ('app', 't`x'), big))
        assert d.is_read_only(d.list_tables_sql(engine, ['app']))
    assert d.checksum_statement(r.ENGINE_MYSQL, ('app', 't'), False) == 'CHECKSUM TABLE `app`.`t`'
    pg = d.checksum_statement(r.ENGINE_POSTGRES, ('dspace', 'public.item'), False)
    assert d.is_read_only(pg) and 'md5' in pg and '"public"."item"' in pg
    body = d.checksum_body(r.ENGINE_MYSQL, [('app', 't', 10), ('app', 'big', 10 ** 9)], {'app.big'})
    assert "CHECKSUM TABLE `app`.`t`" in body and "count:" in body


def test_parse_checksums():
    marked = {'ck:app.t': (0, ['app.t\t12345']), 'ck:app.u': (1, ['ERROR']), 'other': (0, ['x'])}
    assert d.parse_checksums(marked) == {'app.t': '12345', 'app.u': None}


@pytest.fixture
def world(monkeypatch):
    import app.main as main
    from app import drill_service as ds, pitr_service, restore_service as svc
    from app.db.factory import get_database
    db = get_database()
    db.execute("DELETE FROM health_probes WHERE key LIKE 'drill:%%'")
    db.commit()
    job = {'id': 'drj', 'name': 'Drill job', 'pitr_enabled': True, 'backup_type': 'database'}
    monkeypatch.setattr(svc, 'job_context', lambda jid: (dict(job), {'name': 'DB', 'databases': 'app',
                                                                     'password': 'pw-SECRET-1'}, {'name': 's'},
                                                         r.ENGINE_MYSQL, {}))
    monkeypatch.setattr(main, 'get_job_storage_targets', lambda job: [])
    state = {'prod': [{'app.t': '100', 'app.u': '7'}, {'app.t': '100', 'app.u': '8'}], 'copy': {'app.t': '100'}}

    def prod_sql(job, db_config, server, body, timeout=600):
        if 'information_schema.tables' in body:
            out = 'BXSQL-BEGIN t\napp\tt\t10\napp\tu\t5\nBXSQL-END t 0\n'
        else:
            vals = state['prod'].pop(0)
            out = ''.join(f'BXSQL-BEGIN ck:{k}\n{k}\t{v}\nBXSQL-END ck:{k} 0\n' for k, v in vals.items())
        return subprocess.CompletedProcess([], 0, out, '')
    monkeypatch.setattr(svc, 'prod_sql', prod_sql)
    monkeypatch.setattr(svc, 'capture_local', lambda script, timeout=600: subprocess.CompletedProcess(
        [], 0, ''.join(f'BXSQL-BEGIN ck:{k}\n{k}\t{v}\nBXSQL-END ck:{k} 0\n' for k, v in state['copy'].items()), ''))
    monkeypatch.setattr(pitr_service, 'run_tick', lambda jid: (True, 'ok'))
    monkeypatch.setattr(pitr_service, 'load_state', lambda jid: {
        'state': 'ok', 'window_end': datetime.now(timezone.utc) + timedelta(minutes=5)})
    monkeypatch.setattr(svc, 'derive_plan', lambda data: ({'engine': 'mysql', 'method': 'binlog',
                                                           'storage': {'id': 's1', 'name': 'minio'}}, None))
    rows = {}
    monkeypatch.setattr(svc, 'create_row', lambda row: rows.setdefault('row', dict(row)))
    monkeypatch.setattr(svc, 'do_scratch', lambda ctx, row: dict(row, info='{}'))
    removed = []
    monkeypatch.setattr(svc, 'remove_scratch', lambda row, log=None: removed.append(row['id']))
    monkeypatch.setattr(svc, 'update_row', lambda rid, **f: None)
    alerts, history = [], []
    monkeypatch.setattr(main, 'send_notification', lambda *a: alerts.append(a))
    monkeypatch.setattr(main, 'add_history', lambda *a: history.append(a))
    yield {'ds': ds, 'state': state, 'removed': removed, 'alerts': alerts, 'history': history, 'db': db}
    db.execute("DELETE FROM health_probes WHERE key LIKE 'drill:%%'")
    db.commit()


class Ctx:
    id = 'drill-task'
    created_by = 'test'

    def __init__(self):
        import threading
        self.cancel_event = threading.Event()
        self.lines = []

    def log(self, text, stream='stdout'):
        self.lines.append(text)

    def progress(self, pct=None, step=None):
        pass

    def add_secret(self, *a):
        pass


def test_drill_passes_on_quiet_tables_and_records_result(world):
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert res['ok'], res
    assert res['comparison']['compared'] == 1 and res['comparison']['changed'] == ['app.u']
    assert world['removed'] and not world['alerts']
    assert 'passed' in world['history'][-1][3]
    last = world['ds'].serialize(world['ds'].last_result('drj'))
    assert last['ok'] and last['tables_compared'] == 1


def test_drill_mismatch_fails_alerts_and_raises_issue(world):
    world['state']['copy'] = {'app.t': '999'}
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert not res['ok'] and res['comparison']['mismatches'][0]['table'] == 'app.t'
    assert world['alerts'] and 'DIFFER' in world['alerts'][-1][3]
    assert world['removed']
    from app import health
    now = datetime.now(timezone.utc)
    issues = health.detect_pitr({'now': now, 'jobs': {'drj': {'name': 'Drill job', 'pitr_enabled': True}},
                                 'pitr': {}, 'probes': {'drill:drj': {'data': res}}})
    assert any(i.kind == 'restore_drill_failed' and i.fix == 'restore_drill' for i in issues)


def test_drill_infrastructure_error_still_removes_copy_and_records(world, monkeypatch):
    from app import restore_service as svc

    def boom(ctx, row):
        raise tasks.TaskError('restic failed')
    monkeypatch.setattr(svc, 'do_scratch', boom)
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert not res['ok'] and 'restic failed' in res['error'] and world['removed'] and world['alerts']


def test_schedule_all_queues_one_task(world, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'load_jobs', lambda: {'drj': {'pitr_enabled': True, 'backup_type': 'database'},
                                                    'x': {'pitr_enabled': False, 'backup_type': 'database'}})
    db = world['db']
    db.execute("DELETE FROM tasks WHERE kind = 'restore_drill_all'")
    db.commit()
    world['ds'].schedule_all()
    world['ds'].schedule_all()
    rows = db.fetchall("SELECT * FROM tasks WHERE kind = 'restore_drill_all'")
    assert len(rows) == 1
    db.execute("DELETE FROM tasks WHERE kind = 'restore_drill_all'")
    db.commit()

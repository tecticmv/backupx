"""Restore drills: comparison rules, read-only checksums, the drill task with faked restore/SQL."""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app import drill as d, restore as r, tasks


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
    """A MySQL PITR job whose restores, scratch SQL and storage are faked; production helpers explode."""
    import re
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
    monkeypatch.setattr(svc, 'verify_config', lambda job, engine=None: {'drill_count_only_tables': []})
    monkeypatch.setattr(main, 'get_job_storage_targets', lambda job: [])

    prod_calls = []

    def forbidden(name):
        def f(*a, **k):
            prod_calls.append(name)
            raise AssertionError(f'the drill must not call {name}')
        return f
    for name in ('prod_sql', 'run_verify'):
        monkeypatch.setattr(svc, name, forbidden(f'restore_service.{name}'))
    for name in ('_ssh_run', '_ssh_popen', '_build_ssh_cmd_for_server', 'run_backup'):
        if hasattr(main, name):
            monkeypatch.setattr(main, name, forbidden(f'main.{name}'))
    for name in ('run_tick', '_run'):
        monkeypatch.setattr(pitr_service, name, forbidden(f'pitr_service.{name}'))

    anchor_time = '2026-11-01T21:00:05+00:00'
    latest_time = '2026-11-02T03:00:00+00:00'
    plans = []

    def derive_plan(data):
        plans.append(dict(data))
        base = {'engine': 'mysql', 'storage': {'id': 's1', 'name': 'minio'}, 'job_id': data['job_id'],
                'target': 'scratch'}
        if data['point_type'] == 'latest':
            return dict(base, method='binlog', point_type='latest', target_time=latest_time,
                        dump={'snapshot_id': 'dumpsnap1234', 'time': anchor_time}), None
        if data['point_type'] == 'snapshot':
            return dict(base, method='dump', point_type='snapshot', target_time=anchor_time,
                        dump={'snapshot_id': data['snapshot_id'], 'time': anchor_time}), None
        return dict(base, method='binlog', point_type='time', target_time=data['target_time'],
                    dump={'snapshot_id': 'dumpsnap1234', 'time': anchor_time}), None
    monkeypatch.setattr(svc, 'derive_plan', derive_plan)

    # what each scratch copy (A: dump alone, B: PITR to the anchor, C: PITR to latest) contains
    state = {'A': {'app.t': '100', 'app.u': '7'}, 'B': {'app.t': '100', 'app.u': '7'},
             'C': {'app.t': '101', 'app.u': '9', 'app.new': '1'}}
    rows, scratch = {}, []

    def create_row(row):
        rows[row['container']] = dict(row)
        return dict(row)
    monkeypatch.setattr(svc, 'create_row', create_row)

    def do_scratch(ctx, row, verify_production=True):
        scratch.append((row['info']['drill'], verify_production, row['info'].get('compare_production')))
        return dict(row, info='{}')
    monkeypatch.setattr(svc, 'do_scratch', do_scratch)

    def capture_local(script, timeout=600):
        container = re.search(r'backupx-restore-[A-Za-z0-9_-]+', script).group(0)
        label = rows[container]['info']['drill']
        vals = state[label]
        if 'information_schema.tables' in script:
            out = 'BXSQL-BEGIN t\n' + ''.join(f"{k.split('.')[0]}\t{k.split('.')[1]}\t10\n" for k in vals) + \
                'BXSQL-END t 0\n'
        else:
            wanted = re.findall(r'run_sql ck:(\S+)', script)
            out = ''.join(f'BXSQL-BEGIN ck:{k}\n{k}\t{vals[k] if k in vals else "ERR"}\nBXSQL-END ck:{k} '
                          f'{0 if k in vals else 1}\n' for k in wanted)
        return subprocess.CompletedProcess([], 0, out, '')
    monkeypatch.setattr(svc, 'capture_local', capture_local)
    removed = []
    monkeypatch.setattr(svc, 'remove_scratch', lambda row, log=None: removed.append(rows[row['container']]['info']['drill']))
    monkeypatch.setattr(svc, 'update_row', lambda rid, **f: None)
    alerts, history = [], []
    monkeypatch.setattr(main, 'send_notification', lambda *a: alerts.append(a))
    monkeypatch.setattr(main, 'add_history', lambda *a: history.append(a))
    yield {'ds': ds, 'state': state, 'removed': removed, 'alerts': alerts, 'history': history, 'db': db,
           'prod_calls': prod_calls, 'plans': plans, 'scratch': scratch, 'anchor_time': anchor_time,
           'latest_time': latest_time}
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


def test_count_only_keys_by_size_and_per_job_setting():
    tables = [('app', 'small', 10), ('app', 'activity_log', 100), ('app', 'huge', 10 ** 9)]
    keys = d.count_only_keys(r.ENGINE_MYSQL, tables, ['app.activity_log', 'app.not_there'], 5_000_000)
    assert keys == {'app.activity_log', 'app.huge'}
    assert d.count_only_keys(r.ENGINE_MYSQL, tables, [], 5_000_000) == {'app.huge'}


def test_verify_config_normalizes_drill_count_only_tables():
    cfg, errors = r.normalize_verify_config({'drill_count_only_tables': 'epetition.activity_log, bad name;'},
                                            r.ENGINE_MYSQL)
    assert cfg['drill_count_only_tables'] == ['epetition.activity_log'] and errors
    pg, errors = r.normalize_verify_config({'drill_count_only_tables': ['dspace.big']}, r.ENGINE_POSTGRES)
    assert pg['drill_count_only_tables'] == ['dspace.public.big'] and not errors


def test_set_verify_config_keeps_count_only_list_when_absent(monkeypatch):
    from app import restore_service as svc
    stored = {'restore_verify': '{"key_tables": [], "drill_count_only_tables": ["app.activity_log"]}'}
    monkeypatch.setattr(svc, 'job_context', lambda jid: (dict(stored, id=jid), {}, {}, r.ENGINE_MYSQL, {}))
    saved = []

    class FakeDb:
        def execute(self, sql, params):
            saved.append(params)

        def commit(self):
            pass
    monkeypatch.setattr(svc, '_db', lambda: FakeDb())
    cfg = svc.set_verify_config('j', {'key_tables': ['app.t'], 'check_sql': '', 'health_url': ''})
    assert cfg['drill_count_only_tables'] == ['app.activity_log'] and cfg['key_tables'] == ['app.t']
    cfg = svc.set_verify_config('j', {'key_tables': [], 'drill_count_only_tables': []})
    assert cfg['drill_count_only_tables'] == []




def test_compare_copies():
    same = d.compare_copies({'app.a': '1', 'app.b': 'count:5'}, {'app.a': '1', 'app.b': 'count:5'})
    assert same['ok'] and same['identical'] == 2
    bad = d.compare_copies({'app.a': '1', 'app.b': '2'}, {'app.a': '1', 'app.b': '3'})
    assert not bad['ok'] and bad['mismatches'] == [{'table': 'app.b', 'backup': '2', 'pitr': '3'}]
    assert d.compare_copies({'app.a': '1'}, {})['missing'] == ['app.a']
    assert d.compare_copies({'app.a': None}, {'app.a': '1'})['errors'] == ['app.a']
    assert d.compare_copies({'app.a': '1'}, {'app.a': '1', 'app.x': 'present'})['extra'] == ['app.x']
    assert d.readable({'a': 'count:1', 'b': None}) == {'ok': False, 'tables': 2, 'errors': ['b']}
    assert not d.readable({})['ok']


def test_drill_backup_vs_backup_passes_and_never_touches_production(world):
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert res['ok'], res
    assert world['prod_calls'] == []
    # A = the anchored dump alone, B = PITR to the dump's anchor time, C = PITR to the latest point
    kinds = [(p['point_type'], p.get('snapshot_id'), p.get('target_time')) for p in world['plans']]
    assert kinds[0][0] == 'latest'
    assert ('snapshot', 'dumpsnap1234', None) in kinds
    assert ('time', None, world['anchor_time']) in kinds and ('time', None, world['latest_time']) in kinds
    assert [s[0] for s in world['scratch']] == ['A', 'B', 'C']
    assert all(vp is False and cp is False for _l, vp, cp in world['scratch'])  # no prod comparison
    assert world['removed'] == ['A', 'B', 'C'] and not world['alerts']
    assert res['comparison']['identical'] == 2 and res['latest']['ok'] and res['latest']['tables'] == 3
    assert res['mode'] == 'backup-vs-backup' and 'passed' in world['history'][-1][3]
    last = world['ds'].serialize(world['ds'].last_result('drj'))
    assert last['ok'] and last['tables_compared'] == 2 and last['latest']['ok']


def test_drill_never_references_production_helpers():
    import inspect
    from app import drill_service
    src = inspect.getsource(drill_service)
    for name in ('prod_sql', 'prod_runner', '_ssh_run', '_ssh_popen', '_build_ssh_cmd', 'run_tick', 'run_verify',
                 'mysql_connect_args'):
        assert name not in src, name


def test_drill_mismatch_between_backup_and_pitr_fails_and_alerts(world):
    world['state']['B'] = {'app.t': '999', 'app.u': '7'}
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert not res['ok'] and res['comparison']['mismatches'][0]['table'] == 'app.t'
    assert world['alerts'] and 'DIFFER' in world['alerts'][-1][3]
    assert world['removed'] == ['A', 'B', 'C'] and world['prod_calls'] == []
    from app import health
    now = datetime.now(timezone.utc)
    issues = health.detect_pitr({'now': now, 'jobs': {'drj': {'name': 'Drill job', 'pitr_enabled': True}},
                                 'pitr': {}, 'probes': {'drill:drj': {'data': res}}})
    assert any(i.kind == 'restore_drill_failed' and i.fix == 'restore_drill' for i in issues)


def test_drill_latest_point_unreadable_fails(world, monkeypatch):
    from app import restore_service as svc
    real = svc.capture_local

    def broken(script, timeout=600):
        res = real(script, timeout)
        if 'run_sql ck:app.new' in script:
            return subprocess.CompletedProcess([], 0, res.stdout.replace('BXSQL-END ck:app.new 0',
                                                                         'BXSQL-END ck:app.new 1'), '')
        return res
    monkeypatch.setattr(svc, 'capture_local', broken)
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert not res['ok'] and res['comparison']['ok'] and res['latest']['errors'] == ['app.new']
    assert 'NOT readable' in res['summary']


def test_drill_infrastructure_error_still_removes_copy_and_records(world, monkeypatch):
    from app import restore_service as svc

    def boom(ctx, row, verify_production=True):
        raise tasks.TaskError('restic failed')
    monkeypatch.setattr(svc, 'do_scratch', boom)
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert not res['ok'] and 'restic failed' in res['error'] and world['alerts'] and world['prod_calls'] == []


def test_drill_per_job_count_only_table_uses_row_count(world, monkeypatch):
    from app import restore_service as svc
    scripts = []
    real = svc.capture_local
    monkeypatch.setattr(svc, 'capture_local', lambda script, timeout=600: (scripts.append(script),
                                                                           real(script, timeout))[1])
    monkeypatch.setattr(svc, 'verify_config', lambda job, engine=None: {'drill_count_only_tables': ['app.t']})
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert res['ok'], res
    ab = [s for s in scripts if 'run_sql ck:' in s][:2]  # A and B checksums
    assert len(ab) == 2
    for s in ab:
        assert 'count:' in s and 'COUNT(*)) FROM `app`.`t`' in s
        assert 'CHECKSUM TABLE `app`.`t`' not in s and 'CHECKSUM TABLE `app`.`u`' in s


def test_drill_skips_without_scratch_disk(world, monkeypatch):
    from app import health, restore_service as svc
    monkeypatch.setattr(svc, 'derive_plan', lambda data: (
        {'engine': 'mysql', 'method': 'binlog', 'storage': {'id': 's1', 'name': 'minio'},
         'disk': {'ok': False, 'free_bytes': 1, 'needed_bytes': 10}},
        'Not enough free disk on the backupx server: this restore needs about 10 GB'))
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert res['skipped'] and res['ok'] is None and 'Not enough free disk' in res['summary']
    assert not world['scratch'] and world['prod_calls'] == []
    assert not world['alerts'] and world['history'][-1][2] == 'skipped' and not world['removed']
    last = world['ds'].serialize(world['ds'].last_result('drj'))
    assert last['skipped'] and 'free disk' in last['warning']
    now = datetime.now(timezone.utc)
    issues = health.detect_pitr({'now': now, 'jobs': {'drj': {'name': 'Drill job', 'pitr_enabled': True}},
                                 'pitr': {}, 'probes': {'drill:drj': {'data': res}}})
    assert not any(i.kind == 'restore_drill_failed' for i in issues)


def test_scratch_restore_can_skip_the_production_comparison(monkeypatch):
    from app import restore_service as svc
    assert not svc.compares_production({'info': '{}'})  # off unless asked for
    assert svc.compares_production({'info': '{"compare_production": true}'})
    assert not svc.compares_production({'info': '{"compare_production": false}'})
    monkeypatch.setattr(svc, 'job_context', lambda jid: ({'id': jid}, {'databases': 'app'}, {'name': 's'},
                                                         r.ENGINE_MYSQL, {}))
    monkeypatch.setattr(svc, 'verify_config', lambda job, engine=None: {'key_tables': [], 'check_sql': '',
                                                                        'health_url': ''})
    monkeypatch.setattr(svc, 'capture_local', lambda script, timeout=600: subprocess.CompletedProcess([], 0, '', ''))
    called = []
    monkeypatch.setattr(svc, 'prod_sql', lambda *a, **k: called.append(a))
    import app.main as main
    monkeypatch.setattr(main, '_job_secret_values', lambda *a, **k: [])
    v = svc.run_verify({'id': 'x', 'job_id': 'j', 'container': 'c', 'info': '{}'}, production=False)
    assert not called and v['prod_error'].startswith('not compared')


def test_drill_postgres_base_alone_uses_recovery_target_immediate(world, monkeypatch):
    import re
    from app import restore_service as svc
    monkeypatch.setattr(svc, 'job_context', lambda jid: ({'id': 'drj', 'name': 'PG', 'pitr_enabled': True},
                                                         {'databases': 'dspace'}, {'name': 's'},
                                                         r.ENGINE_POSTGRES, {}))
    base_time = '2026-11-01T21:30:00+00:00'

    def derive_plan(data):
        world['plans'].append(dict(data))
        return ({'engine': 'postgres', 'method': 'walg', 'storage': {'id': 's1', 'name': 'minio'},
                 'point_type': data['point_type'], 'target': 'scratch',
                 'target_time': data.get('target_time') or '2026-11-02T03:00:00+00:00',
                 'wal': {'base_backup': 'base_000000010000000A', 'base_time': base_time}}, None)
    monkeypatch.setattr(svc, 'derive_plan', derive_plan)
    seen = []

    def do_scratch(ctx, row, verify_production=True):
        seen.append((row['info']['drill'], row['plan'].get('recovery_target'), row['plan']['target_time'],
                     row['plan']['wal']['base_backup'], verify_production))
        return dict(row, info='{}')
    monkeypatch.setattr(svc, 'do_scratch', do_scratch)
    monkeypatch.setattr(svc, 'capture_local', lambda script, timeout=600: subprocess.CompletedProcess(
        [], 0, ('BXSQL-BEGIN t:dspace\ndspace\tpublic.item\t3\nBXSQL-END t:dspace 0\n' if 'pg_class' in script else
                ''.join(f'BXSQL-BEGIN ck:{k}\n3:abc\nBXSQL-END ck:{k} 0\n'
                        for k in re.findall(r'run_sql ck:(\S+)', script))), ''))
    res = world['ds'].run_drill(Ctx(), 'drj')
    assert res['ok'], res
    assert seen[0] == ('A', 'immediate', base_time, 'base_000000010000000A', False)
    assert seen[1][:3] == ('B', None, base_time) and seen[2][0] == 'C'
    assert world['prod_calls'] == []

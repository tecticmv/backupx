"""
Regression tests for the staging run of 2026-11-02 (and the DSpace investigation).

Each test names the finding it covers (B1..B11, DSpace 1..5); all of them failed
on 7948a83.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app import drill, health, pitr, restore as r
from app import pitr_scripts as scripts
from tests.test_drill import Ctx, world  # noqa: F401  (fixture)
from tests.test_health import base_state, kinds, _ago, NOW
from tests.test_pitr import (DSPACE_IDLE, PG_FACTS, PG_NOW, SERVER, T0, TARGET, UUID, _pg_tick_env)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# DSpace 2 / B5: a failed SSH login or script is an error, never "archiving off"
# ---------------------------------------------------------------------------

def _pg_env_with_run(monkeypatch, fake_run):
    rows = {}
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, PG_FACTS, rows)
    info = json.loads(rows['info'])
    info.update(archiving=True, oldest_base_backup='2026-10-31T02:00:00Z', last_archived_time='1793449186',
                caught_up_at=(PG_NOW - timedelta(minutes=5)).isoformat())
    rows.update(info=json.dumps(info), state=pitr.STATE_OK, alerted_state=None)
    monkeypatch.setattr(ps, '_run', fake_run)
    return ps, job, targets, rows, sent


@pytest.mark.parametrize('stderr,expect', [
    ('@@@@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\nHost key verification failed.',
     'host key changed'),
    ('ssh: connect to host target.example port 22: Connection refused', 'SSH to target.example failed'),
])
def test_pg_tick_ssh_failure_raises_and_keeps_stored_facts(monkeypatch, stderr, expect):
    def fake_run(server, script, timeout):
        return subprocess.CompletedProcess([], 255, '', stderr)
    ps, job, targets, rows, _ = _pg_env_with_run(monkeypatch, fake_run)
    before = rows['info']
    with pytest.raises(ps.PitrError) as e:
        ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert expect in str(e.value)
    assert rows['info'] == before                      # archiving stays on, window untouched
    assert 'streamer_active' not in rows or rows['streamer_active'] == 'active'


def test_pg_tick_incomplete_output_is_an_error(monkeypatch):
    def fake_run(server, script, timeout):  # exit 0 but nothing about archiving (script cut short)
        return subprocess.CompletedProcess([], 0, 'PITR\tuid\t0\n', '')
    ps, job, targets, rows, _ = _pg_env_with_run(monkeypatch, fake_run)
    before = rows['info']
    with pytest.raises(ps.PitrError) as e:
        ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert 'returned no server_version_num' in str(e.value) and rows['info'] == before


def test_record_failure_keeps_info_and_classifies_ssh(monkeypatch):
    from app import pitr_service as ps
    import app.main as main
    rows = {'kind': 'postgres', 'info': json.dumps({'archiving': True, 'last_archived_time': '1793449186'}),
            'consecutive_failures': 2}
    monkeypatch.setattr(ps, 'load_state', lambda job_id: dict(rows))
    monkeypatch.setattr(ps, 'save_state', lambda job_id, **kw: rows.update(kw))
    monkeypatch.setattr(main, 'get_job', lambda job_id: None)
    ps._record_failure('dspace', "SSH to dspace.lan refused: the server's host key changed (REMOTE HOST "
                                 "IDENTIFICATION HAS CHANGED); check it and trust the new key in Health")
    info = json.loads(rows['info'])
    assert info['archiving'] is True and info['failure_kind'] == 'ssh'
    assert rows['consecutive_failures'] == 3


def test_pg_backup_list_failure_keeps_base_backups(monkeypatch):
    def fake_run(server, script, timeout):
        if 'backup-list' in script:
            return subprocess.CompletedProcess([], 1, '', 'ERROR: storage unreachable')
        return subprocess.CompletedProcess([], 0, '\n'.join(f'PITR\t{k}\t{v}' for k, v in PG_FACTS.items()), '')
    ps, job, targets, rows, _ = _pg_env_with_run(monkeypatch, fake_run)
    ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    info = json.loads(rows['info'])
    assert info['oldest_base_backup'] == '2026-10-31T02:00:00Z' and 'storage unreachable' in info['backup_list_error']
    assert rows['state'] == pitr.STATE_OK


def test_skipped_base_backup_writes_history_and_alerts(monkeypatch):
    from app import pitr_service as ps
    import app.main as main
    from contextlib import contextmanager
    history, alerts = [], []

    @contextmanager
    def lock(conn, key):
        yield True
    monkeypatch.setattr('app.scheduler.locks.job_run_lock', lock)
    monkeypatch.setattr(ps, '_job_context', lambda job_id: (
        {'id': job_id, 'name': 'Dspace DB', 'pitr_enabled': True}, dict(SERVER), {}, 'postgres'))
    monkeypatch.setattr(ps, 'load_state', lambda job_id: {'info': json.dumps({'archiving': False}),
                                                         'last_error': None})
    monkeypatch.setattr(main, 'add_history', lambda *a: history.append(a))
    monkeypatch.setattr(main, 'send_notification', lambda *a: alerts.append(a))
    monkeypatch.setattr(ps, '_run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no ssh')))
    ok, message = ps.run_pg_base_backup('dspace')
    assert not ok and 'skipped' in message
    assert history and history[0][2] == 'skipped' and alerts and alerts[0][2] == 'failed'


def test_ssh_failure_text_and_kinds():
    assert pitr.ssh_failure_text(1, 'psql: FATAL', 'h') is None
    assert 'Connection timed out' in pitr.ssh_failure_text(255, 'ssh: connect to host h port 22: Connection timed out', 'h')
    assert pitr.tick_error_kind('SSH to h failed: x') == 'ssh'
    assert pitr.tick_error_kind('shipping failed - MinIO 2: connection refused') == 'ship'
    assert pitr.tick_error_kind('psql: FATAL: password authentication failed') == 'check'


def test_detector_ssh_failure_is_an_ssh_issue_not_archiving_off():
    state = base_state()
    state['jobs']['j1'].update(pitr_enabled=True, backup_type='database')
    state['pitr'] = {'j1': {'kind': 'postgres', 'state': 'ok', 'last_tick_at': _ago(minutes=2),
                            'streamer_active': 'active', 'consecutive_failures': 3,
                            'last_error': 'SSH to web1.lan refused: host key changed',
                            'info': {'archiving': True, 'failure_kind': 'ssh'}}}
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_ssh_failing' and 'web1' in issue.title and 'archiving' not in issue.title
    # fewer failures: no issue yet; never "WAL archiving is not switched on"
    state['pitr']['j1']['consecutive_failures'] = 1
    assert 'wal_archiving_off' not in kinds(health.detect(state))


# ---------------------------------------------------------------------------
# B3 / DSpace 1 + 3: idle PostgreSQL keeps caught_up_at; no flapping
# ---------------------------------------------------------------------------

def test_pg_caught_up_rules():
    now = PG_NOW
    archived = 1793449186  # 12:19:46
    idle = pitr.walg_archiver_health(DSPACE_IDLE)
    one_open = pitr.walg_archiver_health(dict(DSPACE_IDLE, current_wal_file='00000001000000020000003E'))
    unknown = pitr.walg_archiver_health(dict(DSPACE_IDLE, current_wal_file=''))
    prev = now - timedelta(minutes=5)
    assert pitr.pg_caught_up(None, idle, archived, now) == now
    assert pitr.pg_caught_up(prev, one_open, archived, now) == prev        # autovacuum wrote a few bytes
    assert pitr.pg_caught_up(None, one_open, archived, now) == pitr._utc(archived)
    assert pitr.pg_caught_up(prev, unknown, archived, now) == prev         # in recovery / timeline change
    later = pitr._utc(archived) + timedelta(hours=2)                        # busy server archiving
    assert pitr.pg_caught_up(prev - timedelta(hours=3), one_open, int(later.timestamp()), later) == later


def test_idle_pg_with_autovacuum_never_flaps_over_many_ticks(monkeypatch):
    """Idle DSpace, one open segment appearing every few ticks (autovacuum), archive_timeout 15 min."""
    rows = {}
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, PG_FACTS, rows)
    rows.update(state=pitr.STATE_OK, alerted_state=None)
    facts = dict(PG_FACTS)
    seg = 0x3D
    now = PG_NOW
    states = []

    def fake_run(server, script, timeout):
        if 'backup-list' in script:
            return subprocess.CompletedProcess([], 0, json.dumps([{'start_time': '2026-10-31T02:04:42.16Z'}]), '')
        return subprocess.CompletedProcess([], 0, '\n'.join(f'PITR\t{k}\t{v}' for k, v in facts.items()), '')
    monkeypatch.setattr(ps, '_run', fake_run)
    for tick in range(60):  # 5 hours of 5-minute ticks
        now = PG_NOW + timedelta(minutes=5 * tick)
        monkeypatch.setattr(ps, '_now', lambda now=now: now)
        phase = tick % 6
        if phase == 1:   # autovacuum writes into a fresh segment
            facts['current_wal_file'] = f'0000000100000002000000{seg + 1:02X}'
        elif phase == 4:  # archive_timeout switched and archived it
            seg += 1
            facts.update(archiver_last_archived_wal=f'0000000100000002000000{seg:02X}',
                         archiver_last_archived_time=str(int((now - timedelta(minutes=2)).timestamp())),
                         current_wal_file=f'0000000100000002000000{seg:02X}')
        ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
        states.append(rows['state'])
        assert rows['lag_minutes'] is not None and rows['lag_minutes'] <= 15
    assert set(states) == {pitr.STATE_OK} and sent == []


def test_pg_restart_unknown_position_keeps_caught_up_then_lags(monkeypatch):
    rows = {}
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, dict(PG_FACTS, current_wal_file=''), rows)
    info = json.loads(rows['info'])
    info['caught_up_at'] = (PG_NOW - timedelta(minutes=3)).isoformat()
    rows.update(info=json.dumps(info), state=pitr.STATE_OK, alerted_state=None)
    ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert json.loads(rows['info'])['caught_up_at'] == info['caught_up_at'] and rows['state'] == pitr.STATE_OK


def test_pg_lagging_alert_is_pg_specific_without_mirror_error(monkeypatch):
    rows = {}
    facts = dict(PG_FACTS, current_wal_file='000000010000000200000041')  # 4 segments not archived
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, facts, rows, mirror_ok=False)
    rows.update(state=pitr.STATE_OK, alerted_state=None)
    ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert rows['state'] == pitr.STATE_LAGGING
    text = sent[-1][3]
    assert 'WAL archiving is behind' in text and 'mirror' not in text.lower() and 'AccessDenied' not in text


# ---------------------------------------------------------------------------
# DSpace 5: the R2 copy escalates with its age
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('age,severity,alert', [
    (timedelta(minutes=20), 'info', False), (timedelta(hours=2), 'warning', False),
    (timedelta(hours=7), 'critical', True)])
def test_wal_mirror_failure_escalates(age, severity, alert):
    state = base_state()
    state['jobs']['j1'].update(pitr_enabled=True, backup_type='database')
    state['pitr'] = {'j1': {'kind': 'postgres', 'state': 'ok', 'last_tick_at': _ago(minutes=2),
                            'streamer_active': 'active',
                            'info': {'mirror': [{'storage': 'R2', 'ok': False, 'error': '403'}],
                                     'mirror_ok_at': {'R2': (NOW - age).isoformat()}}}}
    [issue] = health.detect(state)
    assert issue.kind == 'wal_mirror_failed' and issue.severity == severity and issue.alert is alert


# ---------------------------------------------------------------------------
# B6: one storage failing to ship is not "binlog streamer stopped"
# ---------------------------------------------------------------------------

def test_ship_failure_to_one_storage_is_its_own_issue():
    state = base_state()
    state['jobs']['j1'].update(pitr_enabled=True, backup_type='database')
    state['pitr'] = {'j1': {'kind': 'mysql', 'state': 'ok', 'last_tick_at': _ago(minutes=2),
                            'streamer_active': 'active', 'heartbeat': _ago(seconds=40),
                            'consecutive_failures': 3, 'last_error': 'shipping failed - MinIO 2: connection refused',
                            'info': {'failure_kind': 'ship', 'ship_failed': {'storages': ['MinIO 2']}}}}
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_ship_failing' and 'MinIO 2' in issue.title and 'streamer' not in issue.title
    assert issue.fix == 'pitr_ship_retry'
    # the streamer really down still wins
    state['pitr']['j1'].update(streamer_active='failed')
    [issue] = health.detect(state)
    assert issue.kind == 'pitr_streamer_down'


def test_ship_error_carries_failing_storages(monkeypatch):
    from app import pitr_service as ps
    import app.main as main
    rows = {'info': '{}', 'consecutive_failures': 0}
    monkeypatch.setattr(ps, 'load_state', lambda job_id: dict(rows))
    monkeypatch.setattr(ps, 'save_state', lambda job_id, **kw: rows.update(kw))
    monkeypatch.setattr(main, 'get_job', lambda job_id: None)
    e = ps.ShipError('shipping failed - MinIO 2: dial tcp: connection refused', storages=['MinIO 2'])
    ps._record_failure('koha', str(e), storages=e.storages)
    info = json.loads(rows['info'])
    assert info['failure_kind'] == 'ship' and info['ship_failed']['storages'] == ['MinIO 2']


# ---------------------------------------------------------------------------
# B1: binlog numbering restarts (RESET MASTER / RESET BINARY LOGS AND GTIDS)
# ---------------------------------------------------------------------------

def _scan(files, events=(), uuid=UUID, closed_all=False, **extra):
    scan = {'files': [{'name': n, 'size': s, 'mtime': m} for n, s, m in files], 'events': list(events),
            'uuid': uuid, 'closed_all': closed_all, 'now': int(T0.timestamp()), 'server_logs': [],
            'server_logs_at': None, 'sync_pos': None, 'reset_pending': ''}
    scan.update(extra)
    return scan


def test_numbering_restart_event_starts_a_new_chain_from_the_first_file():
    state = {'chain_id': 1, 'server_uuid': UUID, 'binlog_base': 'mysql-bin', 'last_seq': 12}
    t = int(T0.timestamp())
    ev = {'line': 1, 'time': '2026-10-31T02:00:30Z',
          'text': 'gap: binlog numbering restarted on the server (the server\'s newest binlog is mysql-bin.000001 '
                  'but it had reached mysql-bin.000012); streaming again from its first binlog mysql-bin.000001'}
    plan = pitr.plan_shipment(state, _scan([('mysql-bin.000001', 9000, t), ('mysql-bin.000002', 400, t)], [ev]))
    assert [b['start_seq'] for b in plan['breaks']] == [1] and pitr.NUMBERING_RESTART in plan['breaks'][0]['reason']
    assert plan['breaks'][0]['at'] == pitr._utc('2026-10-31T02:00:30Z')
    assert plan['segments'][0]['files'] == ['mysql-bin.000001'] and plan['stale'] == []  # not "already shipped"


def test_old_anchor_cannot_anchor_the_chain_after_a_numbering_restart():
    chain = {'chain_id': 2, 'start_seq': 1, 'server_uuid': UUID, 'base': 'mysql-bin',
             'reason': 'streamer: ' + pitr.NUMBERING_RESTART + ' on the server (...)', 'started_at': T0}
    old = {'binlog_file': 'mysql-bin.000012', 'binlog_pos': 4, 'server_uuid': UUID, 'snapshots': {'st1': 'a'},
           'time': T0 - timedelta(hours=2)}
    new = dict(old, binlog_file='mysql-bin.000002', time=T0 + timedelta(hours=1))
    assert not pitr.anchor_usable(old, chain, 1)
    assert pitr.anchor_usable(new, chain, 1)
    # other breaks keep the old rule (no time check)
    assert pitr.anchor_usable(old, dict(chain, reason='missing binlog file(s) x'), 1)


def test_stream_progress_flags_a_frozen_spool_and_a_backwards_server():
    now = int(T0.timestamp())
    state = {'last_seq': 2, 'binlog_base': 'mysql-bin'}
    base = dict(server_logs_at=now - 30, now=now, heartbeat=now - 20)
    # spool frozen at 000003:50000 while the server is at 000004
    scan = _scan([('mysql-bin.000003', 50000, now - 600)],
                 server_logs=[{'name': 'mysql-bin.000003', 'size': 70000}, {'name': 'mysql-bin.000004', 'size': 900}],
                 **base)
    p = pitr.stream_progress({'frozen_ticks': 1}, scan, state, True, 300)
    assert p['behind'] and p['frozen_ticks'] == 2 and not p['in_sync']
    assert p['server_changed_at'] == now - 30
    # caught up: in sync, counter reset
    scan = _scan([('mysql-bin.000004', 1200, now)], server_logs=[{'name': 'mysql-bin.000004', 'size': 900}],
                 sync_pos={'name': 'mysql-bin.000004', 'size': 900, 'at': now - 20}, **base)
    p = pitr.stream_progress({'frozen_ticks': 3}, scan, state, True, 300)
    assert p['in_sync'] and p['frozen_ticks'] == 0 and not p['behind']
    # the heartbeat's verified position is another file: not in sync
    scan['sync_pos'] = {'name': 'mysql-bin.000003', 'size': 900, 'at': now - 20}
    assert not pitr.stream_progress({}, scan, state, True, 300)['in_sync']
    # server numbering went backwards below what was shipped
    scan = _scan([('mysql-bin.000003', 50000, now - 600)], server_logs=[{'name': 'mysql-bin.000001', 'size': 300}],
                 **base)
    p = pitr.stream_progress({}, scan, dict(state, last_seq=2), True, 300)
    assert p['reset'] and not p['in_sync'] and 'numbering restarted' in p['reason']
    # the streamer reported it itself
    scan = _scan([('mysql-bin.000003', 50000, now - 600)], reset_pending='x', **base)
    assert not pitr.stream_progress({}, scan, state, True, 300)['in_sync']


def test_scan_parses_server_listing_sync_pos_and_reset():
    text = '\n'.join(['PITR\tNOW\t1000', 'PITR\tSYNC_POS\tmysql-bin.000004\t900\t990',
                      'PITR\tRESET_PENDING\tour copy of x is bigger', 'PITR\tSERVER_LOGS_AT\t980',
                      'PITR\tSERVER_LOG\tmysql-bin.000004\t900', 'PITR\tSERVER_LOG\tmysql-bin.000003\t5000'])
    scan = pitr.parse_scan_output(text)
    assert scan['sync_pos'] == {'name': 'mysql-bin.000004', 'size': 900, 'at': 990}
    assert scan['reset_pending'] == 'our copy of x is bigger' and scan['server_logs_at'] == 980
    assert [f['name'] for f in scan['server_logs']] == ['mysql-bin.000003', 'mysql-bin.000004']


def test_streamer_detects_numbering_restarts():
    s = scripts.STREAMER_SCRIPT
    for needle in ('reset_reason', 'server_hw', 'same_file', 'reset_pending', 'spool-closed',
                   'binlog numbering restarted on the server', 'sync_pos', 'LOCAL_SIZE" -gt "$CUR_SIZE"'):
        assert needle in s, needle
    # the spool is read before the server is asked (the server can only be ahead)
    loop = s[s.index('while kill -0'):]
    assert loop.index('LOCAL_SIZE=') < loop.index('SHOW BINARY LOGS')
    # gap text must not pin the break to a file ("continuing at" = break point semantics)
    assert 'streaming again from its first binlog' in s


def _bash():
    import shutil
    return shutil.which('bash')


@pytest.mark.skipif(not _bash(), reason='needs bash')
def test_streamer_reset_reason_shell(tmp_path):
    """Run the streamer's reset_reason() against listings."""
    s = scripts.STREAMER_SCRIPT
    funcs = s[s.index('seq_of() {'):s.index('newest_spool()')] + \
        s[s.index('newest_name() {'):s.index('created_line() {')]
    script = f'STATE={tmp_path}\n{funcs}\n' + r'''
printf 'mysql-bin.000001\t5000\nmysql-bin.000002\t7000\nmysql-bin.000003\t300\n' > "$STATE/server_binlogs"
echo mysql-bin.000003 > "$STATE/server_hw"
L1=$(printf 'mysql-bin.000001\t5000\nmysql-bin.000002\t7000\nmysql-bin.000003\t900')
reset_reason "$L1" && echo "R1:yes" || echo "R1:no"
L2=$(printf 'mysql-bin.000001\t400')
reset_reason "$L2" && echo "R2:yes" || echo "R2:no"
L3=$(printf 'mysql-bin.000001\t5000\nmysql-bin.000002\t333\nmysql-bin.000003\t900')
reset_reason "$L3" && echo "R3:yes" || echo "R3:no"
L4=$(printf 'mysql-bin.000001\t5000\nmysql-bin.000002\t7000\nmysql-bin.000003\t100')
reset_reason "$L4" && echo "R4:yes" || echo "R4:no"
'''
    out = subprocess.run(['bash', '-c', script], capture_output=True, text=True).stdout
    assert 'R1:no' in out          # the open file grew: normal
    assert 'R2:yes' in out         # newest below the high-water mark
    assert 'R3:yes' in out         # a closed file changed size
    assert 'R4:yes' in out         # the open file shrank


def test_restore_script_reports_the_newest_replayed_event():
    from tests.test_pitr import JOB
    plan = {'anchor': {'binlog_file': 'mysql-bin.000002', 'binlog_pos': 4, 'snapshots': {'st1': 'd'},
                       'time': T0}, 'segments': [{'snapshots': {'st1': 's'}}], 'partial': None}
    text = scripts.mysql_restore_script(JOB, plan, T0, dict(TARGET), 'r1', 'r2', 'mariadb:10.6', 'mariadb:10.6',
                                        'mariadb')
    assert 'BACKUPX-LAST-EVENT' in text
    assert r.parse_last_event_marker('BACKUPX-LAST-EVENT 261102 15:04:01') == \
        datetime(2026, 11, 2, 15, 4, 1, tzinfo=UTC)
    assert r.parse_last_event_marker('BACKUPX-LAST-EVENT 261102  9:04:01') == datetime(2026, 11, 2, 9, 4, 1,
                                                                                         tzinfo=UTC)
    assert r.parse_last_event_marker('BACKUPX-LAST-EVENT unknown') is None


def test_drill_latest_point_must_reach_the_claimed_window_end():
    dump = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    target = datetime(2026, 11, 2, 15, 23, tzinfo=UTC)
    # frozen chain: data ends 15:04, production kept writing until 15:23
    res = drill.latest_reaches('binlog', target, dump, datetime(2026, 11, 2, 15, 4, tzinfo=UTC),
                               datetime(2026, 11, 2, 15, 22, 30, tzinfo=UTC), 7)
    assert res['ok'] is False and 'miss changes' in res['detail']
    # healthy: replayed up to 15:21, last write seen 15:22:30
    assert drill.latest_reaches('binlog', target, dump, datetime(2026, 11, 2, 15, 21, tzinfo=UTC),
                                datetime(2026, 11, 2, 15, 22, 30, tzinfo=UTC), 7)['ok'] is True
    # idle server: last write before the dump -> nothing to check
    assert drill.latest_reaches('binlog', target, dump, dump - timedelta(days=1), dump - timedelta(hours=5),
                                7)['ok'] is True
    # no record: not checkable (never a failure)
    assert drill.latest_reaches('binlog', target, dump, None, None, 7)['ok'] is None
    assert drill.latest_reaches('walg', target, dump, None, target, 7)['ok'] is None


def test_drill_fails_when_latest_point_is_incomplete(world, monkeypatch):
    from app import drill_service as ds, restore_service as svc
    faked = svc.do_scratch

    def do_scratch(ctx, row, verify_production=True):
        out = faked(ctx, row, verify_production)
        if row['info']['drill'] == 'C':
            out['info'] = json.dumps({'last_event_at': '2026-11-02T01:00:00+00:00'})
        return out
    monkeypatch.setattr(svc, 'do_scratch', do_scratch)
    monkeypatch.setattr(ds, '_server_changed_at', lambda job_id: '2026-11-02T02:59:00+00:00')
    res = ds.run_drill(Ctx(), 'drj')
    assert not res['ok'] and res['latest']['reaches'] is False and 'INCOMPLETE' in res['summary']
    assert world['alerts'] and world['prod_calls'] == []
    # reaching the last write passes
    monkeypatch.setattr(ds, '_server_changed_at', lambda job_id: '2026-11-02T01:03:00+00:00')
    assert ds.run_drill(Ctx(), 'drj')['ok']


# ---------------------------------------------------------------------------
# B2: WAL-G base backups finish at a fraction of a second
# ---------------------------------------------------------------------------

def test_pick_base_with_several_sub_second_base_backups():
    from app import restore_service as svc
    backups = [{'backup_name': 'base_1', 'finish_time': '2026-11-01T02:04:42.160314Z'},
               {'backup_name': 'base_2', 'finish_time': '2026-11-02T02:04:43.987654321Z'},
               {'backup_name': 'base_3', 'finish_time': '2026-11-02T14:00:01.5Z'}]
    # the drill's reference point for base_2 is the next whole second
    ref = drill.ceil_second(backups[1]['finish_time'])
    assert ref == datetime(2026, 11, 2, 2, 4, 44, tzinfo=UTC)
    assert svc._pick_base(backups, ref)['backup_name'] == 'base_2'
    # its truncated finish second was the bug: base_2 hadn't finished yet then
    assert svc._pick_base(backups, ref - timedelta(seconds=1))['backup_name'] == 'base_1'
    assert svc._pick_base(backups, datetime(2026, 11, 3, tzinfo=UTC))['backup_name'] == 'base_3'
    assert drill.ceil_second('2026-11-02T02:04:44Z') == datetime(2026, 11, 2, 2, 4, 44, tzinfo=UTC)


# ---------------------------------------------------------------------------
# B8: the stale-lock age comes from STALE_LOCK_MIN_AGE
# ---------------------------------------------------------------------------

def test_stale_lock_detector_uses_configured_min_age():
    lock = {'id': 'l1', 'time': _ago(minutes=5), 'hostname': 'web1', 'decision': 'auto', 'pid': 1}
    probes = {'repo:j1:a': {'data': {'open': 'ok', 'locks': [lock]}, 'checked_at': NOW}}
    assert 'stale_lock' not in kinds(health.detect(base_state(probes=probes)))           # default 30 min
    issues = health.detect(base_state(probes=probes, stale_lock_min_age=180))
    [issue] = [i for i in issues if i.kind == 'stale_lock']
    assert '3 min' in issue.explanation


# ---------------------------------------------------------------------------
# B7: retention doesn't hold the job's run lock; short lock wait
# ---------------------------------------------------------------------------

def test_retention_runs_after_the_run_lock_is_released(monkeypatch):
    import app.main as main
    from contextlib import contextmanager
    held, events = set(), []

    @contextmanager
    def fake_lock(connect, key):
        if key in held:
            yield False
            return
        held.add(key)
        events.append(('take', key))
        try:
            yield True
        finally:
            held.discard(key)
            events.append(('release', key))
    monkeypatch.setattr(main, 'job_run_lock', fake_lock)
    monkeypatch.setattr(main, '_retention_running', lambda job_id: False)

    def inner(job_id):
        run = main._current_run()
        run.retention = ({'name': 'J'}, [{'name': 'primary'}])
        return True, 'ok'
    monkeypatch.setattr(main, '_run_backup_inner', inner)
    seen = []
    monkeypatch.setattr(main, 'apply_retention', lambda job_id, job, targets: seen.append(set(held)))
    assert main.run_backup('rj1', trigger='manual') == (True, 'ok')
    # forget ran without the job's run lock (Run now isn't refused), under its own retention lock
    assert seen == [{'rj1:retention'}]
    assert events.index(('release', 'rj1')) < events.index(('take', 'rj1:retention'))


def test_finish_backup_queues_retention_inside_a_run(monkeypatch):
    import app.main as main
    for name in ('update_job_progress', 'update_job_status', 'add_history', 'send_notification',
                 'invalidate_snapshot_cache', 'record_run_results'):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    called = []
    monkeypatch.setattr(main, 'apply_retention', lambda *a: called.append(a))
    run = main.BackupRun('rj2')
    main._run_context.run = run
    try:
        main._finish_backup('rj2', {'name': 'J'}, [{'name': 'p'}], main.utc_now(), 0, '', [],
                            [{'exit_code': 0, 'output': []}], 'Backup')
    finally:
        main._run_context.run = None
    assert called == [] and run.retention[1] == [{'name': 'p'}]


def test_forget_after_backup_uses_short_lock_wait(monkeypatch):
    from app import retention
    import app.main as main
    monkeypatch.delenv('RETENTION_FORGET_LOCK_WAIT', raising=False)
    cfg = retention.settings()
    assert cfg['forget_lock_wait'] == '30s' and cfg['lock_wait'] == '10m'
    calls = []

    def fake(job, target, args, timeout, lock_wait=None):
        calls.append(lock_wait)
        return subprocess.CompletedProcess(args, 1, '', 'unable to create lock in backend: repository is already '
                                                         'locked exclusively by PID 1 on web1')
    monkeypatch.setattr(main, '_restic_local', fake)
    with pytest.raises(main.RetentionError) as e:
        main.forget_on_target({'retention_daily': 7}, {'name': 'p'}, cfg)
    assert calls == ['30s'] and 'locked' in str(e.value)

"""
Point-in-time recovery: prerequisite checks, dump anchors, what gets shipped,
chain breaks, restorable windows, alerts, retention by anchor, restore
planning, and that no secret reaches an argv.

Everything here is pure (app.pitr / app.pitr_scripts) or uses the faked
transport from test_secrets_argv; the end-to-end run against real MySQL 8.4 /
MariaDB 11 containers is described in docs/PITR.md.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app import pitr
from app import pitr_scripts as scripts

UTC = timezone.utc
T0 = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
UUID = '420d1619-d4d7-11f1-b1ee-7a453745d646'

DB_PASSWORD = 'db-Pa55 "q\' $x\\y-SECRET'
RESTIC_PASSWORD = 'restic-Pa55-SECRET'
ACCESS_KEY = 'AKIA-ACCESS-SECRET'
SECRET_KEY = 's3-Secret-Key-SECRET'
SECRETS = [DB_PASSWORD, RESTIC_PASSWORD, ACCESS_KEY, SECRET_KEY]


def ts(minutes):
    return int((T0 + timedelta(minutes=minutes)).timestamp())


def f(seq, minutes=0, size=1000, base='mysql-bin'):
    return {'name': f'{base}.{seq:06d}', 'size': size, 'mtime': ts(minutes)}


def scan(files, uuid=UUID, events=(), closed_all=False, **kw):
    return dict({'files': list(files), 'uuid': uuid, 'events': list(events), 'closed_all': closed_all,
                 'active': 'active', 'heartbeat': None, 'now': None}, **kw)


def state(chain_id=1, last_seq=10, uuid=UUID, base='mysql-bin', **kw):
    return dict({'chain_id': chain_id, 'last_seq': last_seq, 'server_uuid': uuid, 'binlog_base': base}, **kw)


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

GOOD_MYSQL = {
    'version': '8.4.8', 'user': 'doadmin@%', 'log_bin': '1', 'binlog_format': 'ROW', 'binlog_row_image': 'FULL',
    'server_id': '1', 'gtid_mode': 'ON', 'binlog_expire_seconds': '0',
    'grants': "GRANT SELECT, RELOAD, PROCESS, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `doadmin`@`%`|",
    'uid': '0', 'user_name': 'root', 'systemd': '1', 'docker': '1', 'free_mb': '50000',
    'binlog_client': 'mysqlbinlog', 'binlog_client_version': 'mysqlbinlog  Ver 8.4.11-11 for Linux on x86_64',
    'dump_client': 'mysqldump', 'dump_client_version': 'mysqldump  Ver 8.4.11-11 for Linux on x86_64',
}


def by_name(checks):
    return {c['check']: c for c in checks}


def test_prereqs_all_ok():
    checks = pitr.evaluate_mysql_prereqs(GOOD_MYSQL)
    assert pitr.prereqs_ok(checks), [c for c in checks if c['status'] == 'fail']
    assert all(set(c) == {'check', 'status', 'current', 'required', 'fix', 'needs_restart'} for c in checks)


def test_prereqs_log_bin_off_needs_restart():
    checks = by_name(pitr.evaluate_mysql_prereqs(dict(GOOD_MYSQL, log_bin='0')))
    row = checks['Binary log (log_bin)']
    assert row['status'] == 'fail' and row['needs_restart'] and 'restart' in row['fix']


def test_prereqs_missing_replication_grant():
    facts = dict(GOOD_MYSQL, grants='GRANT SELECT, RELOAD ON *.* TO `bk`@`%`|')
    checks = by_name(pitr.evaluate_mysql_prereqs(facts))
    assert checks['Privilege REPLICATION SLAVE']['status'] == 'fail'
    assert 'GRANT REPLICATION SLAVE' in checks['Privilege REPLICATION SLAVE']['fix']
    assert checks['Privilege REPLICATION CLIENT']['status'] == 'fail'


def test_prereqs_mariadb_grant_names_count():
    facts = dict(GOOD_MYSQL, version='11.4.2-MariaDB', grants='GRANT RELOAD, BINLOG MONITOR, REPLICATION REPLICA '
                                                              'ON *.* TO `bk`@`%`',
                 binlog_client_version='mariadb-binlog Ver 3.5 Distrib 11.4.2-MariaDB',
                 dump_client_version='mariadb-dump from 11.4.2-MariaDB, client 10.19')
    checks = by_name(pitr.evaluate_mysql_prereqs(facts, mode='docker'))
    assert checks['Privilege REPLICATION SLAVE']['status'] == 'ok'
    assert checks['Privilege REPLICATION CLIENT']['status'] == 'ok'
    assert 'gtid_mode' not in checks


def test_prereqs_mariadb_client_against_mysql_fails_with_image_hint():
    facts = dict(GOOD_MYSQL, binlog_client='mariadb-binlog',
                 binlog_client_version='mariadb-binlog Ver 3.4 Distrib 10.11.6-MariaDB')
    row = by_name(pitr.evaluate_mysql_prereqs(facts))['mysqlbinlog matches the server']
    assert row['status'] == 'fail' and 'percona/percona-server:8.4' in row['fix']


def test_prereqs_old_mysqldump_cannot_anchor_on_84():
    # MySQL 8.4 removed SHOW MASTER STATUS: an 8.0 or MariaDB mysqldump falls back to an unanchored dump
    for version in ('mysqldump  Ver 8.0.46-0ubuntu0.24.04.4 for Linux', 'mysqldump from 10.11.6-MariaDB'):
        row = by_name(pitr.evaluate_mysql_prereqs(dict(GOOD_MYSQL, dump_client_version=version)))[
            'mysqldump can record the binlog position']
        assert row['status'] == 'fail', version
        assert 'percona/percona-server:8.4' in row['fix']


def test_prereqs_connection_error_still_reports_host_checks():
    checks = pitr.evaluate_mysql_prereqs({'connect_error': "Access denied for user 'x'", 'uid': '1000', 'user_name': 'bk',
                                          'sudo_error': 'sudo: a password is required', 'systemd': '1'})
    names = by_name(checks)
    assert names['Database connection']['status'] == 'fail'
    assert names['Root or passwordless sudo']['status'] == 'fail'
    assert not pitr.prereqs_ok(checks)


def test_prereqs_short_server_retention_warns():
    row = by_name(pitr.evaluate_mysql_prereqs(dict(GOOD_MYSQL, binlog_expire_seconds='600')))[
        'Binlog retention on the server']
    assert row['status'] == 'warn'


# Real `--version` first lines (docker images mariadb:10.6/10.11/11.4, the Koha
# host's Ubuntu 22.04 packages, mysql/percona 8.0 and 8.4).
CLIENT_VERSION_LINES = [
    # (binlog line, dump line, server release the client comes from)
    ('mysqlbinlog Ver 3.5 for debian-linux-gnu at x86_64',
     'mysqldump  Ver 10.19 Distrib 10.6.28-MariaDB, for debian-linux-gnu (x86_64)', '10.6.28'),
    ('/usr/bin/mariadb-binlog Ver 3.5 for debian-linux-gnu at x86_64',
     'mysqldump  Ver 10.19 Distrib 10.6.22-MariaDB, for debian-linux-gnu (x86_64)', '10.6.22'),
    ('mariadb-binlog Ver 3.5 for debian-linux-gnu at x86_64',
     'mariadb-dump  Ver 10.19 Distrib 10.11.19-MariaDB, for debian-linux-gnu (x86_64)', '10.11.19'),
    ('mariadb-binlog from 11.4.13-MariaDB, client 3.5 for debian-linux-gnu (x86_64)',
     'mariadb-dump from 11.4.13-MariaDB, client 10.19 for debian-linux-gnu (x86_64)', '11.4.13'),
    ('mysqlbinlog  Ver 8.0.40 for Linux on x86_64 (MySQL Community Server - GPL)',
     'mysqldump  Ver 8.0.40 for Linux on x86_64 (MySQL Community Server - GPL)', '8.0.40'),
    ("mysqlbinlog  Ver 8.0.36-28 for Linux on x86_64 (Percona Server (GPL), Release '28', Revision '47601f19')",
     "mysqldump  Ver 8.0.36-28 for Linux on x86_64 (Percona Server (GPL), Release '28', Revision '47601f19')",
     '8.0.36'),
    ('/usr/bin/mysqlbinlog  Ver 8.4.13 for Linux on x86_64 (MySQL Community Server - GPL)',
     'mysqldump  Ver 8.4.13 for Linux on x86_64 (MySQL Community Server - GPL)', '8.4.13'),
]


@pytest.mark.parametrize('binlog_line,dump_line,release', CLIENT_VERSION_LINES)
def test_client_server_version_reads_the_server_release(binlog_line, dump_line, release):
    assert pitr._client_server_version(dump_line) == release
    binlog_release = pitr._client_server_version(binlog_line)
    if 'Ver 3.' in binlog_line:
        assert binlog_release is None  # 3.5 is mysqlbinlog's own version, not the server's
    else:
        assert binlog_release == release


@pytest.mark.parametrize('binlog_line,dump_line,release', CLIENT_VERSION_LINES)
def test_matching_clients_get_no_binlog_warning(binlog_line, dump_line, release):
    mariadb = 'MariaDB' in dump_line
    server = f'{release}-MariaDB-0ubuntu0.22.04.1-log' if mariadb else release
    binlog_client = binlog_line.split()[0].rsplit('/', 1)[-1]
    facts = dict(GOOD_MYSQL, version=server, binlog_client=binlog_client, binlog_client_version=binlog_line,
                 dump_client='mysqldump', dump_client_version=dump_line)
    checks = by_name(pitr.evaluate_mysql_prereqs(facts))
    row = checks['mysqlbinlog matches the server']
    assert row['status'] == 'ok', row
    assert checks['mysqldump can record the binlog position']['status'] == 'ok'


def test_koha_mariadb_106_binlog_client_no_false_warning():
    # The Koha DB host on 2026-10-31: warned "10.6 or newer" because it read mysqlbinlog's 3.5
    facts = dict(GOOD_MYSQL, version='10.6.22-MariaDB-0ubuntu0.22.04.1-log', binlog_client='mariadb-binlog',
                 binlog_client_version='/usr/bin/mariadb-binlog Ver 3.5 for debian-linux-gnu at x86_64',
                 dump_client='mysqldump',
                 dump_client_version='mysqldump  Ver 10.19 Distrib 10.6.22-MariaDB, for debian-linux-gnu (x86_64)')
    assert by_name(pitr.evaluate_mysql_prereqs(facts))['mysqlbinlog matches the server']['status'] == 'ok'


def test_older_mariadb_client_than_server_still_warns():
    facts = dict(GOOD_MYSQL, version='11.4.13-MariaDB', binlog_client='mariadb-binlog',
                 binlog_client_version='mariadb-binlog Ver 3.5 for debian-linux-gnu at x86_64',
                 dump_client='mariadb-dump',
                 dump_client_version='mariadb-dump  Ver 10.19 Distrib 10.6.28-MariaDB, for debian-linux-gnu (x86_64)')
    row = by_name(pitr.evaluate_mysql_prereqs(facts))['mysqlbinlog matches the server']
    assert row['status'] == 'warn' and 'mariadb:11.4' in row['fix']


def test_older_mysql_client_than_server_still_warns():
    facts = dict(GOOD_MYSQL, binlog_client_version='mysqlbinlog  Ver 8.0.40 for Linux on x86_64 (MySQL Community Server - GPL)')
    row = by_name(pitr.evaluate_mysql_prereqs(facts))['mysqlbinlog matches the server']
    assert row['status'] == 'warn' and 'percona/percona-server:8.4' in row['fix']


def test_unversioned_mariadb_binlog_against_mysql_server_still_fails():
    facts = dict(GOOD_MYSQL, binlog_client='mysqlbinlog',
                 binlog_client_version='mysqlbinlog Ver 3.5 for debian-linux-gnu at x86_64',
                 dump_client_version='mysqldump  Ver 10.19 Distrib 10.6.28-MariaDB, for debian-linux-gnu (x86_64)')
    row = by_name(pitr.evaluate_mysql_prereqs(facts))['mysqlbinlog matches the server']
    assert row['status'] == 'fail'


def test_pg_prereqs_archive_mode_needs_restart():
    facts = {'server_version_num': '100023', 'server_version': '10.23', 'wal_level': 'replica',
             'archive_mode': 'off', 'archive_command': '(disabled)', 'archive_timeout': '0',
             'uid': '0', 'glibc': '2.27'}
    checks = by_name(pitr.evaluate_pg_prereqs(facts))
    assert checks['archive_mode']['status'] == 'fail' and checks['archive_mode']['needs_restart']
    assert not checks['archive_command']['needs_restart']
    assert 'v2.0.1' in checks['WAL-G build']['fix']


def test_walg_build_selection():
    assert pitr.walg_build_for('2.27', 10)[0] == 'v2.0.1'      # Ubuntu 18.04 / PG 10 (DSpace)
    assert pitr.walg_build_for('2.35', 10)[0] == 'v2.0.1'      # newer distro, still PG 10
    assert pitr.walg_build_for('2.35', 16)[0] == 'v3.0.9'
    assert pitr.walg_build_for('2.17', 10) is None


def test_postgres_conf_changes_never_include_restart_commands():
    lines = '\n'.join(pitr.postgres_conf_changes())
    assert "archive_mode = on" in lines and pitr.WALG_ARCHIVE_COMMAND in lines and 'archive_timeout = 900' in lines


# ---------------------------------------------------------------------------
# Dump anchor
# ---------------------------------------------------------------------------

def test_anchor_mysql_84():
    header = """-- MySQL dump 10.13  Distrib 8.4.11
SET @@SESSION.SQL_LOG_BIN= 0;
SET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ '420d1619-d4d7-11f1-b1ee-7a453745d646:1-16,
7a9b0000-0000-0000-0000-000000000000:1-3';
--
-- Position to start replication or point-in-time recovery from
--

-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='mysql-bin.000005', SOURCE_LOG_POS=1318;
"""
    anchor = pitr.parse_dump_anchor(header)
    assert anchor['file'] == 'mysql-bin.000005' and anchor['pos'] == 1318 and anchor['seq'] == 5
    assert anchor['gtid_set'] == ('420d1619-d4d7-11f1-b1ee-7a453745d646:1-16,'
                                  '7a9b0000-0000-0000-0000-000000000000:1-3')


def test_anchor_mariadb_and_old_mysql():
    anchor = pitr.parse_dump_anchor("-- CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.000002', MASTER_LOG_POS=5692;")
    assert (anchor['file'], anchor['pos'], anchor['gtid_set']) == ('mysql-bin.000002', 5692, '')
    anchor = pitr.parse_dump_anchor("CHANGE MASTER TO MASTER_LOG_FILE='binlog.000123', MASTER_LOG_POS=4;\n"
                                    "-- SET GLOBAL gtid_slave_pos='0-1-42';")
    assert anchor['seq'] == 123 and anchor['gtid_set'] == '0-1-42'


def test_anchor_missing_or_bogus():
    assert pitr.parse_dump_anchor('-- MySQL dump 10.13\nCREATE TABLE x (id int);') is None
    assert pitr.parse_dump_anchor("-- CHANGE MASTER TO MASTER_LOG_FILE='../etc/passwd', MASTER_LOG_POS=4;") is None


def test_extract_anchor_lines_from_script_output():
    lines = ['backupx-anchor-time: 1793412000',
             "backupx-anchor: -- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='mysql-bin.000009', SOURCE_LOG_POS=3098;",
             'Initializing new restic repository...']
    anchor, error, rest = pitr.extract_anchor_lines(lines)
    assert anchor['file'] == 'mysql-bin.000009' and anchor['time'] == datetime.fromtimestamp(1793412000, UTC)
    assert error is None and rest == ['Initializing new restic repository...']


def test_extract_anchor_failure_is_reported():
    anchor, error, rest = pitr.extract_anchor_lines(
        ["backupx-anchor-failed: dump with the binlog position failed (Couldn't execute 'SHOW MASTER STATUS')"])
    assert anchor is None and 'SHOW MASTER STATUS' in error and rest == []


# ---------------------------------------------------------------------------
# What gets shipped
# ---------------------------------------------------------------------------

def test_first_run_starts_chain_and_ships_only_closed_files():
    plan = pitr.plan_shipment({}, scan([f(5, 1), f(6, 2), f(7, 3)]))
    assert [s['files'] for s in plan['segments']] == [['mysql-bin.000005', 'mysql-bin.000006']]
    assert plan['partial']['name'] == 'mysql-bin.000007'  # open file: copied, not deleted
    assert plan['breaks'] == [{'chain_id': 1, 'start_seq': 5, 'reason': 'start', 'server_uuid': UUID,
                               'base': 'mysql-bin'}]
    assert plan['last_seq'] == 6 and plan['chain_id'] == 1


def test_open_file_only_is_never_shipped_as_closed():
    plan = pitr.plan_shipment(state(last_seq=10), scan([f(11, 5)]))
    assert plan['segments'] == [] and plan['partial']['name'] == 'mysql-bin.000011'
    assert plan['breaks'] == []


def test_unchanged_open_file_is_not_copied_again():
    st = state(last_seq=10, partial_file='mysql-bin.000011', partial_size=1000, partial_mtime=ts(5))
    plan = pitr.plan_shipment(st, scan([f(11, 5)]))
    assert plan['partial'] is None and plan['open']['name'] == 'mysql-bin.000011'


def test_closed_all_ships_the_newest_file_too():
    plan = pitr.plan_shipment(state(last_seq=10), scan([f(11, 1), f(12, 2)], closed_all=True))
    assert plan['segments'][0]['files'] == ['mysql-bin.000011', 'mysql-bin.000012']
    assert plan['partial'] is None and plan['open'] is None


def test_restreamed_copies_of_shipped_files_are_stale():
    plan = pitr.plan_shipment(state(last_seq=11), scan([f(10), f(11), f(12), f(13)]))
    assert plan['stale'] == ['mysql-bin.000010', 'mysql-bin.000011']
    assert plan['segments'][0]['files'] == ['mysql-bin.000012']


def test_seq_numbers_sort_numerically():
    s = pitr.parse_scan_output('PITR\tFILE\tmysql-bin.1000000\t5\t10\nPITR\tFILE\tmysql-bin.999999\t5\t9\n')
    assert [x['name'] for x in s['files']] == ['mysql-bin.999999', 'mysql-bin.1000000']


def test_parse_scan_output():
    text = '\n'.join([
        'noise from a login banner',
        'PITR\tNOW\t1793412000', 'PITR\tACTIVE\tactive', f'PITR\tUUID\t{UUID}',
        'PITR\tHEARTBEAT\t1793411990', 'PITR\tFILE\tmysql-bin.000005\t1318\t1793411000',
        'PITR\tEVENT\t1\t2026-10-31T03:30:13Z gap: mysql-bin.000012 was purged; continuing at mysql-bin.000015',
        'PITR\tCLOSED_ALL\t1',
    ])
    s = pitr.parse_scan_output(text)
    assert s['now'] == 1793412000 and s['active'] == 'active' and s['uuid'] == UUID
    assert s['files'] == [{'name': 'mysql-bin.000005', 'size': 1318, 'mtime': 1793411000}]
    assert s['events'][0]['text'].startswith('gap:') and s['events'][0]['time'] == '2026-10-31T03:30:13Z'
    assert s['closed_all'] is True


def test_parse_ship_output_attributes_snapshots_per_storage():
    out = '\n'.join([
        '{"message_type":"backupx_target","index":0}',
        '{"message_type":"backupx_pitr","what":"segment","index":0}',
        '{"message_type":"summary","snapshot_id":"aaa"}',
        '{"message_type":"backupx_pitr","what":"partial","index":0}',
        '{"message_type":"summary","snapshot_id":"ppp"}',
        '{"message_type":"backupx_target_done","index":0,"exit_code":0}',
        '{"message_type":"backupx_target","index":1}',
        'Fatal: unable to open config file',
        '{"message_type":"backupx_target_done","index":1,"exit_code":1}',
    ])
    results, shipped = pitr.parse_ship_output(out, 2)
    assert results[0]['segments'] == {0: 'aaa'} and results[0]['partial'] == 'ppp'
    assert results[1]['exit_code'] == 1 and 'unable to open config' in results[1]['output'][0]
    assert shipped is False


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def test_missing_file_breaks_chain():
    plan = pitr.plan_shipment(state(last_seq=10), scan([f(11), f(14), f(15), f(16)]))
    assert [s['files'] for s in plan['segments']] == [['mysql-bin.000011'], ['mysql-bin.000014', 'mysql-bin.000015']]
    assert [s['chain_id'] for s in plan['segments']] == [1, 2]
    assert plan['breaks'][0]['start_seq'] == 14
    assert 'mysql-bin.000012..mysql-bin.000013' in plan['breaks'][0]['reason']


def test_missing_file_before_open_file_breaks_chain():
    plan = pitr.plan_shipment(state(last_seq=10), scan([f(13)]))
    assert plan['breaks'][0]['start_seq'] == 13 and plan['chain_id'] == 2


def test_server_uuid_change_breaks_chain():
    plan = pitr.plan_shipment(state(last_seq=900), scan([f(1), f(2)], uuid='new-uuid'))
    assert plan['breaks'][0]['reason'].startswith('server_uuid changed')
    assert plan['breaks'][0]['start_seq'] == 1 and plan['server_uuid'] == 'new-uuid'
    assert plan['segments'][0]['chain_id'] == 2 and plan['segments'][0]['files'] == ['mysql-bin.000001']


def test_streamer_gap_event_breaks_even_when_numbering_is_contiguous():
    # The file being streamed (12) was purged before its tail arrived; the server went on
    # with 13: no hole in the numbers, but 12 is incomplete -> break at 13.
    ev = [{'line': 1, 'time': '', 'text': 'gap: mysql-bin.000012 was purged on the server before it was fully '
                                          'streamed; continuing at mysql-bin.000013'}]
    plan = pitr.plan_shipment(state(last_seq=11), scan([f(12), f(13), f(14)], events=ev))
    assert [(s['files'], s['chain_id']) for s in plan['segments']] == [
        (['mysql-bin.000012'], 1), (['mysql-bin.000013'], 2)]
    assert plan['breaks'][0]['start_seq'] == 13 and plan['breaks'][0]['reason'].startswith('streamer:')
    assert plan['ack_events'] == 1


def test_streamer_gap_event_on_the_open_file():
    ev = [{'line': 3, 'time': '', 'text': 'gap: binlogs after mysql-bin.000011 were purged; continuing at '
                                          'mysql-bin.000014'}]
    plan = pitr.plan_shipment(state(last_seq=11), scan([f(14)], events=ev))
    assert plan['breaks'][0]['start_seq'] == 14 and plan['last_seq'] == 13


def test_non_gap_events_are_acknowledged_without_break():
    ev = [{'line': 2, 'time': '', 'text': 'note: something'}]
    plan = pitr.plan_shipment(state(last_seq=10), scan([f(11), f(12)], events=ev))
    assert plan['breaks'] == [] and plan['ack_events'] == 2


def test_base_name_change_breaks_chain():
    plan = pitr.plan_shipment(state(last_seq=10), scan([f(1, base='binlog'), f(2, base='binlog')]))
    assert plan['breaks'][0]['reason'].startswith('binlog base name changed')
    assert plan['base'] == 'binlog'


# ---------------------------------------------------------------------------
# Windows and state
# ---------------------------------------------------------------------------

def chain(cid=1, start=5, reason='start', uuid=UUID):
    return {'chain_id': cid, 'start_seq': start, 'reason': reason, 'server_uuid': uuid, 'base': 'mysql-bin'}


def seg(first, last, end_min, cid=1, sid=None, forgotten=False, snaps=None, kind='closed'):
    return {'id': sid or first, 'chain_id': cid, 'first_seq': first, 'last_seq': last,
            'first_file': f'mysql-bin.{first:06d}', 'last_file': f'mysql-bin.{last:06d}',
            'end_time': T0 + timedelta(minutes=end_min), 'forgotten': forgotten, 'kind': kind,
            'snapshots': snaps if snaps is not None else {'s1': f'snap{first}'}}


def anchor(seq, minute, aid=None, uuid=UUID, snaps=None, pos=4):
    return {'id': aid or seq, 'binlog_file': f'mysql-bin.{seq:06d}', 'binlog_pos': pos,
            'time': T0 + timedelta(minutes=minute), 'server_uuid': uuid,
            'snapshots': snaps if snaps is not None else {'s1': f'dump{seq}'}, 'forgotten': False}


def test_window_from_anchor_to_newest_data():
    windows = pitr.chain_windows([chain()], [seg(5, 6, 10), seg(7, 9, 30)], [anchor(6, 8)],
                                 {'chain_id': 1, 'seq': 10, 'until': T0 + timedelta(minutes=34)})
    w = windows[0]
    assert w['anchored'] and w['from'] == T0 + timedelta(minutes=8) and w['to'] == T0 + timedelta(minutes=34)


def test_anchor_from_another_server_is_not_usable():
    windows = pitr.chain_windows([chain()], [seg(5, 9, 30)], [anchor(6, 8, uuid='other')])
    assert not windows[0]['anchored']


def test_anchor_whose_dump_was_forgotten_is_not_usable():
    windows = pitr.chain_windows([chain()], [seg(5, 9, 30)], [anchor(6, 8, snaps={})])
    assert not windows[0]['anchored']


def test_state_ok_lagging_broken_unanchored():
    now = T0 + timedelta(minutes=36)
    ok = pitr.chain_windows([chain()], [seg(5, 9, 30)], [anchor(6, 8)])
    assert pitr.derive_state(True, True, ok, now, 15)[0] == pitr.STATE_OK
    assert pitr.derive_state(True, True, ok, now + timedelta(minutes=30), 15)[0] == pitr.STATE_LAGGING
    assert pitr.derive_state(True, True, ok, now, 15, streamer_live=False)[0] == pitr.STATE_LAGGING
    unanchored = pitr.chain_windows([chain()], [seg(5, 9, 30)], [])
    assert pitr.derive_state(True, True, unanchored, now, 15)[0] == pitr.STATE_UNANCHORED
    broken = pitr.chain_windows([chain(), chain(2, 12, 'missing binlog file(s) mysql-bin.000010..000011')],
                                [seg(5, 9, 30), seg(12, 13, 35, cid=2)], [anchor(6, 8)])
    state_, window, _ = pitr.derive_state(True, True, broken, now, 15)
    assert state_ == pitr.STATE_BROKEN and window['chain_id'] == 1  # earlier times stay restorable
    assert pitr.derive_state(False, True, ok, now, 15)[0] == pitr.STATE_DISABLED
    assert pitr.derive_state(True, False, [], now, 15)[0] == pitr.STATE_PENDING


def test_new_anchor_repairs_broken_chain():
    windows = pitr.chain_windows([chain(), chain(2, 12, 'gap')],
                                 [seg(5, 9, 30), seg(12, 13, 50, cid=2)], [anchor(6, 8), anchor(12, 40)])
    assert pitr.derive_state(True, True, windows, T0 + timedelta(minutes=52), 15)[0] == pitr.STATE_OK


def test_streamer_health():
    assert pitr.streamer_health('active', None, 1000, 300) == (False, True)   # just started: no false alarm
    assert pitr.streamer_health('active', 900, 1000, 300) == (True, True)
    assert pitr.streamer_health('active', 100, 1000, 300) == (False, False)  # stalled
    assert pitr.streamer_health('failed', 990, 1000, 300) == (False, False)


def test_lag_threshold():
    assert pitr.lag_threshold_minutes(5) == 15
    assert pitr.lag_threshold_minutes(10) == 30
    assert pitr.lag_threshold_minutes(5, configured=7) == 7
    assert pitr.lag_threshold_minutes(5, kind='postgres') == 25


# ---------------------------------------------------------------------------
# Alerts: only on state changes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('new, alerted, expect', [
    (pitr.STATE_OK, None, (None, None)),
    (pitr.STATE_LAGGING, None, ('failed', pitr.STATE_LAGGING)),
    (pitr.STATE_LAGGING, pitr.STATE_LAGGING, (None, pitr.STATE_LAGGING)),   # still lagging: quiet
    (pitr.STATE_BROKEN, pitr.STATE_LAGGING, ('failed', pitr.STATE_BROKEN)),  # got worse: alert again
    (pitr.STATE_OK, pitr.STATE_BROKEN, ('recovered', None)),
    (pitr.STATE_PENDING, pitr.STATE_LAGGING, (None, pitr.STATE_LAGGING)),
    (pitr.STATE_UNANCHORED, None, ('failed', pitr.STATE_UNANCHORED)),
])
def test_alert_decision(new, alerted, expect):
    assert pitr.alert_decision(new, alerted) == expect


def test_unanchored_right_after_enabling_waits_a_nightly_cycle():
    now = T0 + timedelta(hours=30)
    assert not pitr.should_alert_unanchored(now - timedelta(hours=2), now)
    assert pitr.should_alert_unanchored(now - timedelta(hours=27), now)


def test_refresh_state_alerts_once_and_recovers(monkeypatch):
    from app import pitr_service as ps
    rows = {'kind': 'mysql', 'last_tick_at': T0, 'state': pitr.STATE_OK, 'alerted_state': None,
            'streamer_active': 'active'}
    sent = []
    monkeypatch.setattr(ps, 'load_state', lambda job_id: dict(rows))
    monkeypatch.setattr(ps, 'save_state', lambda job_id, **kw: rows.update(kw))
    windows = {'value': pitr.chain_windows([chain()], [seg(5, 9, 30)], [anchor(6, 8)])}
    monkeypatch.setattr(ps, 'compute_windows', lambda job_id, state=None: windows['value'])
    import app.main as main
    monkeypatch.setattr(main, 'send_notification', lambda *a: sent.append(a))
    clock = {'now': T0 + timedelta(minutes=35)}
    monkeypatch.setattr(ps, '_now', lambda: clock['now'])
    job = {'name': 'E-Petition', 'pitr_enabled': True, 'pitr_interval': 5}

    assert ps.refresh_state('epet', job) == pitr.STATE_OK and sent == []
    clock['now'] = T0 + timedelta(minutes=60)          # nothing new shipped for 30 min
    assert ps.refresh_state('epet', job) == pitr.STATE_LAGGING
    assert ps.refresh_state('epet', job) == pitr.STATE_LAGGING
    assert [s[2] for s in sent] == ['failed']           # one alert, not one per tick
    windows['value'] = pitr.chain_windows([chain()], [seg(5, 12, 59)], [anchor(6, 8)])
    assert ps.refresh_state('epet', job) == pitr.STATE_OK
    assert [s[2] for s in sent] == ['failed', 'recovered']
    assert 'PITR' in sent[0][1]


# ---------------------------------------------------------------------------
# Retention by anchor
# ---------------------------------------------------------------------------

def test_retention_keeps_back_to_oldest_anchor_in_period():
    now = T0 + timedelta(days=10)
    segments = [seg(1, 3, 0, sid=1), seg(4, 6, 60 * 24 * 2, sid=2), seg(7, 9, 60 * 24 * 5, sid=3),
                seg(10, 12, 60 * 24 * 9, sid=4)]
    anchors = [anchor(2, 0, aid=1), anchor(5, 60 * 24 * 4, aid=2), anchor(8, 60 * 24 * 6, aid=3)]
    plan = pitr.retention_plan([chain(start=1)], segments, anchors, now, 7)
    assert plan['cutoff_anchor']['id'] == 2          # oldest dump within 7 days (day 4)
    assert [s['id'] for s in plan['forget']] == [1]  # segment 4-6 holds the anchor's file: kept


def test_retention_without_anchor_in_period_forgets_nothing():
    now = T0 + timedelta(days=30)
    plan = pitr.retention_plan([chain(start=1)], [seg(1, 3, 0), seg(4, 6, 60)], [anchor(2, 0)], now, 7)
    assert plan == {'cutoff_anchor': None, 'forget': []}


def test_retention_forgets_older_chains_and_skips_partials():
    now = T0 + timedelta(days=3)
    segments = [seg(1, 3, 0, sid=1), seg(5, 5, 10, sid=2, kind='partial'), seg(10, 12, 60 * 24 * 2, cid=2, sid=3)]
    anchors = [anchor(10, 60 * 24, aid=9)]
    plan = pitr.retention_plan([chain(start=1), chain(2, 10, 'gap')], segments, anchors, now, 7)
    assert plan['cutoff_anchor']['id'] == 9
    assert [s['id'] for s in plan['forget']] == [1]


def test_retention_ignores_anchors_whose_dump_is_gone():
    now = T0 + timedelta(days=3)
    plan = pitr.retention_plan([chain(start=1)], [seg(1, 3, 0), seg(4, 6, 60 * 24)],
                               [anchor(5, 60 * 24, snaps={})], now, 7)
    assert plan['forget'] == []


# ---------------------------------------------------------------------------
# Restore planning
# ---------------------------------------------------------------------------

def test_plan_restore_picks_latest_dump_before_target():
    segs = [seg(5, 6, 10), seg(7, 8, 20), seg(9, 10, 30)]
    anchors = [anchor(5, 2, aid=1), anchor(7, 15, aid=2)]
    plan = pitr.plan_restore(T0 + timedelta(minutes=18), [chain()], segs, anchors, 's1')
    assert plan['anchor']['id'] == 2
    assert [s['first_seq'] for s in plan['segments']] == [7]


def test_plan_restore_uses_open_file_copy():
    partial = dict(seg(9, 9, 40, kind='partial'), until=T0 + timedelta(minutes=45))
    plan = pitr.plan_restore(T0 + timedelta(minutes=44), [chain()], [seg(5, 8, 20)], [anchor(5, 2)], 's1',
                             [partial])
    assert plan['partial']['first_seq'] == 9


def test_plan_restore_rejects_uncovered_times():
    with pytest.raises(pitr.RestorePlanError, match='only reach'):
        pitr.plan_restore(T0 + timedelta(minutes=50), [chain()], [seg(5, 8, 20)], [anchor(5, 2)], 's1')
    with pytest.raises(pitr.RestorePlanError, match='no dump before'):
        pitr.plan_restore(T0 + timedelta(minutes=1), [chain()], [seg(5, 8, 20)], [anchor(5, 2)], 's1')
    with pytest.raises(pitr.RestorePlanError):  # snapshots only on another storage
        pitr.plan_restore(T0 + timedelta(minutes=10), [chain()], [seg(5, 8, 20)], [anchor(5, 2)], 'r2')


def test_plan_restore_after_break_needs_new_anchor():
    chains = [chain(), chain(2, 12, 'gap')]
    segs = [seg(5, 9, 30), seg(12, 14, 60, cid=2)]
    with pytest.raises(pitr.RestorePlanError):
        pitr.plan_restore(T0 + timedelta(minutes=55), chains, segs, [anchor(5, 2)], 's1')
    plan = pitr.plan_restore(T0 + timedelta(minutes=25), chains, segs, [anchor(5, 2)], 's1')
    assert plan['chain_id'] == 1


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

def test_pitr_settings_validation():
    fields, error = pitr.pitr_settings_from_request({'pitr_enabled': True, 'pitr_interval': '5',
                                                     'pitr_client_image': 'percona/percona-server:8.4'})
    assert error is None and fields['pitr_interval'] == 5 and fields['pitr_retention_days'] == 7
    assert pitr.pitr_settings_from_request({'pitr_interval': 0})[1]
    assert pitr.pitr_settings_from_request({'pitr_retention_days': 'x'})[1]
    assert pitr.pitr_settings_from_request({'pitr_client_image': 'mysql:8.4; rm -rf /'})[1]
    fields, _ = pitr.pitr_settings_from_request({}, {'pitr_enabled': True, 'pitr_interval': 10})
    assert fields['pitr_enabled'] is True and fields['pitr_interval'] == 10


def test_connection_server_id_is_stable_and_high():
    assert pitr.connection_server_id('epet') == pitr.connection_server_id('epet')
    assert pitr.connection_server_id('epet') != pitr.connection_server_id('koha')
    assert 3_000_000_000 <= pitr.connection_server_id('epet') < 2 ** 32


# ---------------------------------------------------------------------------
# No secrets in argv
# ---------------------------------------------------------------------------

DB_CONFIG = {'id': 'db1', 'type': 'mysql', 'host': 'db.example.ondigitalocean.com', 'port': 25060,
             'username': 'doadmin', 'password': DB_PASSWORD, 'databases': '*', 'docker_container': ''}
TARGET = {'id': 'st1', 'name': 'primary', 'endpoint': 's3.example.com', 'bucket': 'bucket',
          'access_key': ACCESS_KEY, 'secret_key': SECRET_KEY, 'skip_ssl_verify': False, 'status': 'active'}
SERVER = {'id': 'srv1', 'name': 'srv', 'host': 'target.example', 'ssh_port': 22, 'ssh_user': 'root',
          'ssh_auth_type': 'key_path', 'ssh_key': '/k'}
JOB = {'id': 'epet', 'name': 'E-Petition', 'backup_type': 'database', 'backup_prefix': 'epet',
       'restic_password': RESTIC_PASSWORD, 'server_id': 'srv1', 'database_config_id': 'db1',
       'pitr_enabled': True, 'pitr_interval': 5, 'pitr_retention_days': 7, 'pitr_client_image': ''}


def assert_clean_argv(args):
    text = '\0'.join(str(a) for a in args)
    for secret in SECRETS:
        assert secret not in text, f'secret in argv: {args!r}'


class Recorder:
    def __init__(self, stdout=''):
        self.calls = []
        self.stdout = stdout

    def run(self, args, input=None, env=None, **kw):
        self.calls.append({'args': args, 'stdin': input or '', 'env': env})
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr='')


@pytest.fixture
def recorder(monkeypatch):
    import app.main as main
    rec = Recorder()
    monkeypatch.setattr(main.subprocess, 'run', rec.run)
    return rec


def test_prereq_check_sends_password_over_stdin_only(recorder, monkeypatch):
    from app import pitr_service as ps
    import app.main as main
    monkeypatch.setattr(main, 'get_job', lambda job_id: dict(JOB))
    monkeypatch.setattr(main, 'get_db_config', lambda cid: dict(DB_CONFIG))
    monkeypatch.setattr(main, 'get_server', lambda sid: dict(SERVER))
    ps.check_prereqs('epet')
    assert len(recorder.calls) == 1
    assert_clean_argv(recorder.calls[0]['args'])
    assert recorder.calls[0]['args'][-1] == main.REMOTE_SHELL
    assert 'db-Pa55' in recorder.calls[0]['stdin']


def test_tick_scripts_keep_secrets_out_of_argv(recorder, monkeypatch):
    from app import pitr_service as ps
    import app.main as main
    recorder.stdout = '\n'.join([f'PITR\tUUID\t{UUID}', 'PITR\tACTIVE\tactive', 'PITR\tNOW\t1793412000',
                                 'PITR\tFILE\tmysql-bin.000005\t100\t1793411000',
                                 'PITR\tFILE\tmysql-bin.000006\t100\t1793411900'])
    monkeypatch.setattr(main, 'get_job', lambda job_id: dict(JOB))
    monkeypatch.setattr(main, 'get_db_config', lambda cid: dict(DB_CONFIG))
    monkeypatch.setattr(main, 'get_server', lambda sid: dict(SERVER))
    monkeypatch.setattr(main, 'get_job_storage_targets', lambda job, include_inactive=False: [dict(TARGET)])
    monkeypatch.setattr(main, 'ensure_restic_installed', lambda *a, **kw: (True, 'ok'))
    monkeypatch.setattr(ps, 'load_state', lambda job_id: {})
    with pytest.raises(ps.PitrError):  # the fake ship output confirms nothing: shipping "fails"
        ps._tick('epet')
    assert len(recorder.calls) >= 2
    for call in recorder.calls:
        assert_clean_argv(call['args'])
    ensure, ship = recorder.calls[0]['stdin'], recorder.calls[1]['stdin']
    assert 'db-Pa55' in ensure                      # the option file content travels in the script
    assert RESTIC_PASSWORD in ship and SECRET_KEY in ship
    assert '-binlog' in ship                        # sibling repository


def test_database_backup_with_pitr_anchor_flags(monkeypatch):
    import app.main as main
    rec = Recorder()

    class _Proc:
        def __init__(self, args, **kw):
            rec.calls.append({'args': args, 'env': kw.get('env')})
            self.stdin = self
            self.stdout = iter([])
            self.stderr = None
            self.returncode = 0
            self.data = []

        def write(self, text):
            self.data.append(text)

        def close(self):
            pass

        def wait(self, timeout=None):
            import time
            for _ in range(100):
                if self.data:
                    break
                time.sleep(0.01)
            return 0

    procs = []
    monkeypatch.setattr(main.subprocess, 'Popen', lambda args, **kw: procs.append(_Proc(args, **kw)) or procs[-1])
    for name in ('update_job_progress', 'update_job_status', 'add_history', 'send_notification',
                 'invalidate_snapshot_cache', 'apply_retention'):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    monkeypatch.setattr(main, 'get_server', lambda sid: dict(SERVER))
    monkeypatch.setattr(main, 'ensure_db_client_installed', lambda server, client: (True, 'ok'))
    recorded = []
    monkeypatch.setattr(main.pitr_service, 'record_anchor', lambda *a: recorded.append(a))
    for image in ('', 'percona/percona-server:8.4'):
        procs.clear()
        monkeypatch.setattr(main, 'load_db_configs', lambda: [dict(DB_CONFIG)])
        main.run_database_backup('epet', dict(JOB, pitr_client_image=image), [dict(TARGET)])
        assert_clean_argv(rec.calls[-1]['args'])
        script = ''.join(procs[0].data)
        assert '--source-data=2' in script and '--master-data=2' in script and '$PITR_FLAG' in script
        assert 'PITR_OFF=1' in script                           # falls back to a plain dump
        assert '--tag pitr-anchor' in script and '$ANCHOR_TAGS' in script
        assert ' -p' not in script.split('--single-transaction', 1)[0].rsplit('\n', 1)[-1]
        if image:
            assert 'docker run --rm -i --network host --entrypoint sh percona/percona-server:8.4' in \
                script.replace('$DOCKER', 'docker')
            assert '< "$MYCNF"' in script                       # option file on stdin
    assert recorded and recorded[-1][0] == 'epet'


def test_restore_script_embeds_no_secret():
    plan = {'anchor': dict(anchor(5, 2), snapshots={'st1': 'dump5'}),
            'segments': [dict(seg(5, 6, 10), snapshots={'st1': 'bin5'})], 'partial': None, 'chain_id': 1}
    text = scripts.mysql_restore_script(dict(JOB), plan, T0 + timedelta(minutes=8), dict(TARGET),
                                        's3:https://s3.example.com/bucket/epet',
                                        's3:https://s3.example.com/bucket/epet-binlog',
                                        'mysql:8.4', 'percona/percona-server:8.4', 'mysql')
    for secret in SECRETS:
        assert secret not in text
    assert "TARGET_TIME='2026-10-31 02:08:00'" in text and 'START_POS=4' in text
    assert '--skip-gtids' in text and 'dump5' in text and 'bin5' in text


def test_scripts_embed_secrets_only_as_quoted_literals():
    script = scripts.ensure_and_scan_script('epet', dict(DB_CONFIG))
    # The option file is written through printf | sh -c 'cat > file': never as an argument
    assert "printf '%s' \"$3\" | $SUDO sh -c 'umask 077; cat > \"$1\"' sh \"$tmp\"" in script
    assert 'password=' not in scripts.mysql_env('epet', dict(DB_CONFIG))


BASH = shutil.which('bash')


@pytest.mark.skipif(BASH is None, reason='bash not available')
def test_generated_shell_parses():
    plan = {'anchor': dict(anchor(5, 2), snapshots={'st1': 'dump5'}),
            'segments': [dict(seg(5, 6, 10), snapshots={'st1': 'bin5'})],
            'partial': dict(seg(7, 7, 12, kind='partial'), snapshots={'st1': 'p7'}), 'chain_id': 1}
    texts = {
        'streamer': scripts.STREAMER_SCRIPT,
        'ensure': scripts.ensure_and_scan_script('epet', dict(DB_CONFIG)),
        'ensure-docker': scripts.ensure_and_scan_script('koha', dict(DB_CONFIG, docker_container='koha-db')),
        'ship': scripts.ship_script('epet', [pitr._segment([f(5, 1), f(6, 2)], 1)], f(7, 3), ['mysql-bin.000004'],
                                    2, RESTIC_PASSWORD, 'BACKUPX_FAILED=0\npitr_ship\n'),
        'prereq': scripts.mysql_prereq_script(dict(DB_CONFIG), 'percona/percona-server:8.4'),
        'teardown': scripts.teardown_script('epet'),
        'restore': scripts.mysql_restore_script(dict(JOB), plan, T0 + timedelta(minutes=11), dict(TARGET),
                                                'r1', 'r2', 'mariadb:11.4', 'mariadb:11.4', 'mariadb'),
        'walg-run': scripts.walg_run_script('wal-g backup-push "$PGDATA"'),
        'pg-prereq': scripts.pg_prereq_script(),
    }
    for name, text in texts.items():
        proc = subprocess.run([BASH, '-n'], input=text, capture_output=True, text=True)
        assert proc.returncode == 0, f'{name}: {proc.stderr}'


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------

def test_scheduler_has_own_pitr_executor():
    import app.main as main
    sched = main._new_scheduler('UTC')
    assert 'pitr' in sched._executors and 'default' in sched._executors


def test_desired_pitr_schedules(monkeypatch):
    import app.main as main
    configs = {'db1': {'type': 'mysql'}, 'db2': {'type': 'postgresql'}}
    monkeypatch.setattr(main, 'get_db_config', lambda cid: configs.get(cid))
    jobs = {
        'epet': dict(JOB, pitr_interval=5),
        'dspace': dict(JOB, database_config_id='db2', schedule_enabled=True, schedule_cron='0 2 * * *'),
        'files': {'backup_type': 'filesystem', 'pitr_enabled': True},
        'off': dict(JOB, pitr_enabled=False),
    }
    desired = main._desired_pitr_schedules(jobs)
    assert set(desired) == {'epet__pitr', 'dspace__pitr', 'dspace__walg_base'}
    assert desired['epet__pitr'][1:3] == ('interval', 5)
    assert desired['dspace__walg_base'][1:3] == ('cron', '0 2 * * *')


@pytest.mark.skipif(BASH is None, reason='bash not available')
def test_walg_scripts_parse():
    storage = dict(TARGET, endpoint='http://rustfs.local:9000')
    for name, text in {
        'walg-install': scripts.walg_install_script(storage, 'dspace'),
        'pg-restore': scripts.pg_restore_script(dict(JOB, id='dspace'), T0, storage, 'dspace'),
    }.items():
        proc = subprocess.run([BASH, '-n'], input=text, capture_output=True, text=True)
        assert proc.returncode == 0, f'{name}: {proc.stderr}'
    restore = scripts.pg_restore_script(dict(JOB, id='dspace'), T0, storage, 'dspace')
    assert ACCESS_KEY not in restore and SECRET_KEY not in restore
    assert 'postgres:10-bullseye' in restore  # wal-g needs glibc >= 2.27 inside the scratch server
    install = scripts.walg_install_script(storage, 'dspace')
    assert 'pg_ctl' not in install and 'systemctl restart' not in install and 'ALTER SYSTEM' not in install


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def session_client(client):
    """Logged in through the session, not POST /api/auth/login (its rate limit is shared by all tests)."""
    with client.session_transaction() as sess:
        sess['_user_id'] = 'testadmin'
        sess['_fresh'] = True
    return client


def test_pitr_routes_need_a_job(session_client):
    authenticated_client = session_client
    assert authenticated_client.get('/api/jobs/nope/pitr').status_code == 404
    assert authenticated_client.post('/api/jobs/nope/pitr/check').status_code == 404
    assert authenticated_client.post('/api/jobs/nope/pitr/restore-script', json={}).status_code == 404


def test_pitr_only_for_database_jobs(session_client):
    authenticated_client = session_client
    res = authenticated_client.post('/api/jobs', json={'job_id': 'fs-pitr', 'name': 'x', 'backup_type': 'filesystem',
                                                       'pitr_enabled': True})
    assert res.status_code == 400 and 'database' in res.get_json()['error']
    res = authenticated_client.post('/api/jobs', json={'job_id': 'db-pitr', 'name': 'x', 'backup_type': 'database',
                                                       'pitr_enabled': True, 'pitr_interval': 99})
    assert res.status_code == 400 and 'interval' in res.get_json()['error']


# ---------------------------------------------------------------------------
# Prerequisite fixes: GRANT account, binlog client start errors, root/sudo
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('current_user, account', [
    ('azureuser@localhost', "'azureuser'@'localhost'"),
    ('doadmin@%', "'doadmin'@'%'"),
    ('bk@10.0.%', "'bk'@'10.0.%'"),
    ('bk@192.168.1.0/255.255.255.0', "'bk'@'192.168.1.0/255.255.255.0'"),
    ('bk@db.example.com', "'bk'@'db.example.com'"),
    ('we@ird@host', "'we@ird'@'host'"),               # user names may contain '@', hosts never
    ("o'neil@%", "'o''neil'@'%'"),
    ("'bk'@'%'", "'bk'@'%'"),
    ('`bk`@`localhost`', "'bk'@'localhost'"),
    ('bk', "'bk'@'%'"),
    ('', "'user'@'%'"),
])
def test_mysql_account(current_user, account):
    assert pitr.mysql_account(current_user) == account


def test_prereqs_grant_fix_names_the_real_account():
    facts = dict(GOOD_MYSQL, user='azureuser@localhost', grants='GRANT SELECT ON *.* TO `azureuser`@`localhost`|')
    checks = by_name(pitr.evaluate_mysql_prereqs(facts))
    fix = checks['Privilege REPLICATION SLAVE']['fix']
    assert fix == "GRANT REPLICATION SLAVE ON *.* TO 'azureuser'@'localhost';"
    assert "TO 'azureuser'@'localhost';" in checks['Privilege RELOAD']['fix']
    assert "@localhost'@" not in fix


MARIADB_103 = dict(GOOD_MYSQL, version='10.3.39-MariaDB-0ubuntu0.20.04.2', gtid_mode='',
                   grants='GRANT RELOAD, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `bk`@`localhost`|',
                   binlog_client='mysqlbinlog',
                   binlog_client_version='mysqlbinlog Ver 3.4 for debian-linux-gnu at x86_64',
                   dump_client='mysqldump',
                   dump_client_version='mysqldump  Ver 10.19 Distrib 10.3.39-MariaDB, for debian-linux-gnu (x86_64)')
CHARSET_ERROR = "mysqlbinlog: unknown variable 'default-character-set=utf8mb4'"


def test_prereqs_binlog_client_that_cannot_start_is_not_a_version_mismatch():
    facts = dict(MARIADB_103, binlog_client_version='', binlog_client_error=CHARSET_ERROR,
                 binlog_host_defaults_error=CHARSET_ERROR)
    checks = by_name(pitr.evaluate_mysql_prereqs(facts))
    assert 'mysqlbinlog matches the server' not in checks
    row = checks['mysqlbinlog starts']
    assert row['status'] == 'fail' and CHARSET_ERROR in row['current']
    assert '[client]' in row['fix'] and '[mysql]' in row['fix'] and '50-client.cnf' in row['fix']
    assert 'mariadb:10.3' in row['fix']


def test_prereqs_host_option_file_error_only_warns_when_streamer_can_start():
    # snipe-it / old-website: plain `mysqlbinlog` dies on [client] default-character-set,
    # `mysqlbinlog --defaults-file=<ours>` (what the streamer runs) is fine
    facts = dict(MARIADB_103, binlog_host_defaults_error=CHARSET_ERROR)
    checks = pitr.evaluate_mysql_prereqs(facts)
    rows = by_name(checks)
    assert rows['mysqlbinlog reads the host option files']['status'] == 'warn'
    assert 'default-character-set' in rows['mysqlbinlog reads the host option files']['fix']
    assert rows['mysqlbinlog matches the server']['status'] == 'ok'
    assert pitr.prereqs_ok(checks)


def test_binlog_start_hint_generic():
    assert "'ssl-mode'" in pitr.binlog_start_hint("mysqlbinlog: unknown variable 'ssl-mode=REQUIRED'",
                                                  '10.3.39-MariaDB')
    assert 'fix the error above' in pitr.binlog_start_hint('Segmentation fault', '')


@pytest.mark.parametrize('facts, status, text', [
    ({'uid': '0', 'user_name': 'root'}, 'ok', 'root'),
    ({'uid': '1000', 'user_name': 'azureuser', 'sudo_uid': '0'}, 'ok', 'sudo -n works'),
    ({'uid': '1000', 'user_name': 'bk', 'sudo_error': 'sudo: a password is required'}, 'fail',
     'password is required'),
    ({'uid': '1000', 'user_name': 'bk', 'sudo_uid': '1001'}, 'fail', 'no passwordless sudo'),  # not to root
    ({'uid': '1000', 'user_name': 'bk'}, 'fail', 'no passwordless sudo'),
    ({}, 'fail', 'not checked'),
    ({'is_root': '1', 'sudo': '1'}, 'fail', 'not checked'),  # unverified flags count for nothing
])
def test_root_check_needs_evidence(facts, status, text):
    pg_base = {'server_version_num': '100023', 'server_version': '10.23', 'wal_level': 'replica', 'glibc': '2.27'}
    for evaluate, base in ((pitr.evaluate_mysql_prereqs, GOOD_MYSQL), (pitr.evaluate_pg_prereqs, pg_base)):
        merged = {k: v for k, v in base.items() if k not in ('uid', 'user_name')}
        row = by_name(evaluate(dict(merged, **facts)))['Root or passwordless sudo']
        assert row['status'] == status and text in row['current'], (evaluate.__name__, row)


def _stub(directory, name, body):
    path = directory / name
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)


@pytest.mark.skipif(BASH is None or sys.platform == 'win32', reason='needs bash on a POSIX host')
@pytest.mark.parametrize('sudo_ok', [False, True])
def test_mysql_prereq_script_measures_sudo_and_binlog_start(tmp_path, sudo_ok):
    """Run the real prerequisite script against stub programs: a MariaDB 10.3 host whose
    [client] section breaks plain mysqlbinlog, reached as a non-root SSH user."""
    _stub(tmp_path, 'id', 'case "$1" in -u) echo 1000;; -un) echo bk;; -gn) echo bk;; *) echo "uid=1000(bk)";; esac\n')
    _stub(tmp_path, 'sudo', 'echo 0; exit 0\n' if sudo_ok else 'echo "sudo: a password is required" >&2; exit 1\n')
    _stub(tmp_path, 'mysqlbinlog',
          'case "$1" in --defaults-file=*) ;; *) echo "' + CHARSET_ERROR + '" >&2; exit 7;; esac\n'
          'for a; do [ "$a" = --version ] && echo "mysqlbinlog Ver 3.4 for debian-linux-gnu at x86_64"; done\n'
          'exit 0\n')
    _stub(tmp_path, 'mysqldump', 'echo "' + MARIADB_103['dump_client_version'] + '"\n')
    _stub(tmp_path, 'mysql', 'for a; do last="$a"; done\n'
          'case "$last" in *"SHOW GRANTS"*) echo "' + MARIADB_103['grants'].rstrip('|') + '"; exit 0;; esac\n'
          'printf "version\\t10.3.39-MariaDB\\nuser\\tbk@localhost\\nlog_bin\\t1\\nbinlog_format\\tROW\\n'
          'server_id\\t1\\nbinlog_expire_seconds\\t172800\\n"\n')
    _stub(tmp_path, 'docker', 'exit 1\n')
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')
    proc = subprocess.run([BASH, '-s'], input=scripts.mysql_prereq_script(dict(DB_CONFIG, docker_container='')),
                          capture_output=True, text=True, env=env, timeout=60)
    facts = pitr.parse_kv_output(proc.stdout)
    assert facts.get('uid') == '1000' and facts.get('user_name') == 'bk', proc.stdout + proc.stderr
    checks = by_name(pitr.evaluate_mysql_prereqs(facts))
    root = checks['Root or passwordless sudo']
    if sudo_ok:
        assert root['status'] == 'ok' and 'sudo -n works' in root['current']
    else:
        assert root['status'] == 'fail' and 'password is required' in root['current']
    assert 'default-character-set' in facts['binlog_host_defaults_error']
    assert 'binlog_client_error' not in facts                       # the --defaults-file run started
    assert checks['mysqlbinlog matches the server']['status'] == 'ok'
    assert checks['mysqlbinlog reads the host option files']['status'] == 'warn'


def test_streamer_runs_mysqlbinlog_with_only_its_own_option_file():
    lines = [line for line in scripts.STREAMER_SCRIPT.splitlines() if 'pitr_client' in line and '"$BINLOG"' in line]
    assert len(lines) == 2
    for line in lines:
        # --defaults-file must be the first option; --no-defaults + --defaults-extra-file is rejected
        assert '"$BINLOG" --defaults-file="$CNF"' in line, line
        assert '--defaults-extra-file' not in line and '--no-defaults' not in line
    assert scripts.STREAMER_VERSION != '1'  # changed content: installed hosts get the new script + restart


# ---------------------------------------------------------------------------
# WAL-G: an idle server is not lagging
# ---------------------------------------------------------------------------

DSPACE_IDLE = {  # DSpace 2026-10-31 13:41 UTC: nothing written since 3D was archived at 12:19
    'archiver_last_archived_wal': '00000001000000020000003D', 'archiver_last_archived_time': '1793449186',
    'archiver_failed_count': '0', 'archiver_last_failed_wal': '', 'archiver_last_failed_epoch': '',
    'current_wal_file': '00000001000000020000003D',  # pg_walfile_name('2/3E000000'): segment boundary
    'current_wal_lsn': '2/3E000000', 'wal_segment_size': '16777216',
}


def test_walg_archiver_health_idle_open_backlog_failing():
    h = pitr.walg_archiver_health(DSPACE_IDLE)
    assert h['idle'] and h['pending'] == 0 and not h['failing']
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, current_wal_file='00000001000000020000003E'))
    assert not h['idle'] and h['pending'] == 1                  # open segment, archived at archive_timeout
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, current_wal_file='000000010000000200000041'))
    assert h['pending'] == 4
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, archiver_last_archived_wal='0000000100000002000000FF',
                                       current_wal_file='000000010000000300000000'))
    assert h['pending'] == 1                                    # crosses the xlogid boundary (16 MB segments)
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, wal_segment_size='',
                                       current_wal_file='00000001000000020000003E'))
    assert h['pending'] == 1                                    # default 16 MB when unknown
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, archiver_failed_count='3',
                                       archiver_last_failed_wal='00000001000000020000003E',
                                       archiver_last_failed_epoch='1793449800',
                                       current_wal_file='00000001000000020000003E'))
    assert h['failing'] and not h['idle'] and 'archive_command failing' in h['detail']
    # an old failure followed by successes is history, not a failure
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, archiver_failed_count='1',
                                       archiver_last_failed_epoch='1793440000'))
    assert not h['failing'] and h['idle']
    h = pitr.walg_archiver_health(dict(DSPACE_IDLE, current_wal_file=''))
    assert h['pending'] is None and not h['idle']               # unknown position: time-based lag as before


def test_derive_state_complete_at_extends_freshness():
    window = [{'chain_id': 1, 'reason': 'start', 'from': T0, 'to': T0 + timedelta(minutes=10), 'anchored': True,
               'anchor_ids': []}]
    now = T0 + timedelta(minutes=90)
    assert pitr.derive_state(True, True, window, now, 25)[0] == pitr.STATE_LAGGING
    idle = [dict(window[0], complete_at=(now - timedelta(minutes=2)).isoformat())]
    state_, _, lag = pitr.derive_state(True, True, idle, now, 25)
    assert state_ == pitr.STATE_OK and round(lag) == 2
    stale = [dict(window[0], complete_at=(now - timedelta(minutes=40)).isoformat())]  # ticks stopped
    assert pitr.derive_state(True, True, stale, now, 25)[0] == pitr.STATE_LAGGING


PG_NOW = datetime(2026, 10, 31, 13, 45, tzinfo=UTC)
PG_FACTS = dict(DSPACE_IDLE, server_version_num='100023', server_version='10.23', wal_level='replica',
                archive_mode='on', archive_command='/usr/local/bin/wal-g-archive.sh %p', archive_timeout='15min')


def _pg_tick_env(monkeypatch, facts, rows, mirror_ok=True):
    from app import pitr_service as ps
    import app.main as main
    r2 = dict(TARGET, id='st2', name='Cloudflare R2', endpoint='r2.example.com')
    job = dict(JOB, id='dspace', name='Dspace DB', backup_prefix='dspace')
    sent, mirrored = [], []
    monkeypatch.setattr(ps, 'load_state', lambda job_id: dict(rows))
    monkeypatch.setattr(ps, 'save_state', lambda job_id, **kw: rows.update(kw))
    monkeypatch.setattr(main, 'send_notification', lambda *a: sent.append(a))
    monkeypatch.setattr(ps, '_now', lambda: PG_NOW)

    def fake_run(server, script, timeout):
        if 'backup-list' in script:
            out = json.dumps([{'start_time': '2026-10-31T12:04:42.160314Z'}])
        else:
            out = '\n'.join(f'PITR\t{k}\t{v}' for k, v in facts.items())
        return subprocess.CompletedProcess([], 0, out, '')
    monkeypatch.setattr(ps, '_run', fake_run)

    def fake_mirror(prefix, primary, others):
        mirrored.append(prefix)
        return [{'storage': 'Cloudflare R2', 'ok': mirror_ok, 'error': '' if mirror_ok else 'AccessDenied'}]
    monkeypatch.setattr(ps, '_mirror_wal', fake_mirror)
    targets = [dict(TARGET), r2]
    t = targets[0]
    info = {'walg_version': 'wal-g version v2.0.1',
            'walg_env_hash': ps._hash(t['endpoint'], t['bucket'], t['access_key'], t['secret_key'], 'dspace'),
            'verify_at': '2026-10-31T13:30:00+00:00', 'integrity': 'OK'}
    rows.update({'kind': 'postgres', 'info': json.dumps(info), 'last_tick_at': T0, 'state': pitr.STATE_LAGGING,
                 'alerted_state': pitr.STATE_LAGGING, 'streamer_active': 'active'})
    return ps, job, targets, sent, mirrored


def test_pg_tick_idle_server_recovers_and_records_a_ship(monkeypatch):
    rows = {}
    ps, job, targets, sent, mirrored = _pg_tick_env(monkeypatch, PG_FACTS, rows)
    ok, message = ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert ok and 'idle' in message
    assert rows['state'] == pitr.STATE_OK and rows['lag_minutes'] == 0
    assert rows.get('last_ship_at') is not None
    assert mirrored == ['dspace'] and rows['last_error'] is None
    assert [s[2] for s in sent] == ['recovered']                    # it had alerted "lagging"
    assert rows['alerted_state'] is None
    # the window end stays the real last archived time (what a restore can replay to)
    assert pitr._utc(rows['window_end']) == pitr._utc(1793449186)


def test_pg_tick_failing_archiver_is_lagging_with_reason(monkeypatch):
    rows = {}
    facts = dict(PG_FACTS, current_wal_file='00000001000000020000003E', archiver_failed_count='7',
                 archiver_last_failed_wal='00000001000000020000003E', archiver_last_failed_epoch='1793452000',
                 archiver_last_failed_time='2026-10-31 13:06:40+00')
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, facts, rows)
    rows.update(state=pitr.STATE_OK, alerted_state=None)
    ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert rows['state'] == pitr.STATE_LAGGING and rows['streamer_active'] == 'failing'
    assert 'archive_command failing' in rows['last_error'] and 'last_ship_at' not in rows
    assert [s[2] for s in sent] == ['failed'] and 'archive_command failing' in sent[0][3]


def test_pg_tick_mirror_failure_is_not_a_ship(monkeypatch):
    rows = {}
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, PG_FACTS, rows, mirror_ok=False)
    rows.update(state=pitr.STATE_OK, alerted_state=None)
    ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert 'last_ship_at' not in rows and 'WAL mirror failed' in rows['last_error']
    assert rows['state'] == pitr.STATE_OK                           # the primary still has every WAL


def test_pg_tick_writing_server_keeps_time_based_lag(monkeypatch):
    rows = {}
    facts = dict(PG_FACTS, current_wal_file='00000001000000020000003E')  # data written, not archived yet
    ps, job, targets, sent, _ = _pg_tick_env(monkeypatch, facts, rows)
    ps._pg_tick('dspace', job, dict(SERVER), {'type': 'postgresql'}, targets)
    assert rows['state'] == pitr.STATE_LAGGING                      # 86 min since the last archive
    assert json.loads(rows['info'])['idle_at'] is None

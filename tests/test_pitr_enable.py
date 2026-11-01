"""
Enable-PITR wizard (P3): remediation planning, the maintenance flow (validate ->
restart -> health check -> rollback), the scheduled window, the one-time admin
credential handoff and the health hooks.

SSH is faked here (scripts are answered by what they contain); the end-to-end
rig (docs/UX-OVERHAUL.md) runs the real thing against throwaway containers.
"""
import json
import time as _time
from datetime import datetime, timedelta, timezone

import pytest

from app import credentials, maint_scripts as ms, pitr_enable as pe, tasks

ADMIN_PW = 'Admin-Secret-pw9'
DB_PW = 'Db-Secret-pw1'


# =============================================================================
# Fixture facts
# =============================================================================

def maria_checks(log_bin='OFF', fmt='MIXED', reload=False, charset=True):
    from app import pitr
    facts = {'version': '10.3.39-MariaDB-0ubuntu0.20.04.2', 'user': 'app@localhost', 'log_bin': log_bin,
             'binlog_format': fmt, 'binlog_row_image': 'FULL', 'server_id': '1', 'binlog_expire_seconds': '0',
             'grants': "GRANT USAGE ON *.* TO `app`@`localhost`|GRANT ALL PRIVILEGES ON `app`.* TO `app`@`localhost`"
             + ('|GRANT RELOAD ON *.* TO `app`@`localhost`' if reload else ''),
             'uid': '0', 'user_name': 'root', 'systemd': '1', 'docker': '1', 'free_mb': '9000',
             'binlog_client': 'mysqlbinlog', 'binlog_client_version': 'mysqlbinlog Ver 3.4 for debian-linux-gnu',
             'dump_client': 'mysqldump',
             'dump_client_version': 'mysqldump  Ver 10.17 Distrib 10.3.39-MariaDB, for debian-linux-gnu (x86_64)'}
    if charset:
        facts['binlog_host_defaults_error'] = "mysqlbinlog: unknown variable 'default-character-set=utf8mb4'"
    return facts, pitr.evaluate_mysql_prereqs(facts)


MARIA_HOST = {'uid': '0', 'systemd': '1', 'local_ips': '172.20.0.5 bxhost', 'docker': '1', 'unit': 'mariadb.service',
              'mysqld_version': 'mysqld  Ver 10.3.39-MariaDB', 'includedirs': '/etc/mysql/conf.d /etc/mysql/mariadb.conf.d',
              'dropin_dirs': '/etc/mysql/conf.d /etc/mysql/mariadb.conf.d',
              'charset_files': '/etc/mysql/mariadb.conf.d/50-client.cnf', 'charset_value': 'utf8mb4',
              'error_log': ''}


def bundle(facts, checks, host, kind='mysql'):
    from app import pitr
    return {'kind': kind, 'checks': checks, 'ok': pitr.prereqs_ok(checks), 'facts': facts,
            'server_version': facts.get('version'), 'docker': facts.get('docker') == '1',
            'suggested_client_image': pitr.suggested_client_image(facts.get('version', '')), 'host': host,
            'checked_at': datetime.now(timezone.utc).isoformat()}


DB = {'name': 'Koha DB', 'host': '127.0.0.1', 'port': 3306, 'username': 'app', 'password': DB_PW}
SERVER = {'name': 'koha-host', 'host': 'bxhost'}


# =============================================================================
# Pure: targets and items
# =============================================================================

def test_mariadb_host_target_uses_last_included_confd_and_can_restart():
    _f, _c = maria_checks()
    t = pe.mysql_target(DB, SERVER, {'server_version': '10.3.39-MariaDB'}, MARIA_HOST)
    assert t['install'] == 'host' and t['config_method'] == 'dropin'
    assert t['config_path'] == '/etc/mysql/mariadb.conf.d/zz-backupx-pitr.cnf'
    assert t['restart_supported'] and t['service'] == 'mariadb.service'
    # MySQL's chain: conf.d first, mysql.conf.d last -> our file goes to mysql.conf.d
    t2 = pe.mysql_target(DB, SERVER, {'server_version': '8.4.3'},
                         dict(MARIA_HOST, unit='mysql.service', includedirs='/etc/mysql/conf.d /etc/mysql/mysql.conf.d',
                              dropin_dirs='/etc/mysql/conf.d /etc/mysql/mysql.conf.d'))
    assert t2['config_path'] == '/etc/mysql/mysql.conf.d/zz-backupx-pitr.cnf'


def test_managed_remote_and_docker_targets():
    t = pe.mysql_target(dict(DB, host='db-mysql-fra1-1234.b.db.ondigitalocean.com'), SERVER, {}, MARIA_HOST)
    assert t['install'] == 'managed' and t['managed_provider'] == 'DigitalOcean' and not t['restart_supported']
    t = pe.mysql_target(dict(DB, host='10.9.9.9'), SERVER, {}, MARIA_HOST)
    assert t['install'] == 'remote' and not t['restart_supported']
    host = dict(MARIA_HOST, container_found='1',
                docker_mounts='/var/lib/mysql|/srv/koha/db|true|bind;/etc/mysql/conf.d|/srv/koha/conf|true|bind;')
    t = pe.mysql_target(dict(DB, docker_container='koha-db'), SERVER, {}, host)
    assert t['install'] == 'docker' and t['config_method'] == 'bind_mount'
    assert t['config_path'] == '/srv/koha/conf/zz-backupx-pitr.cnf' and t['restart_supported']
    t = pe.mysql_target(dict(DB, docker_container='koha-db'), SERVER, {},
                        dict(host, docker_mounts='/var/lib/mysql|/srv/koha/db|true|bind;'))
    assert t['config_method'] == 'manual' and not t['restart_supported']
    assert 'never recreates' in t['restart_unsupported_reason']


def test_mariadb_log_bin_off_items_fold_into_one_restart():
    facts, checks = maria_checks()
    b = bundle(facts, checks, MARIA_HOST)
    t = pe.mysql_target(DB, SERVER, b, MARIA_HOST)
    items = {i['id']: i for i in pe.mysql_items(b, t, MARIA_HOST, {})}
    lb = items['mysql.log_bin']
    assert lb['downtime'] == 'restart' and lb['auto']
    assert lb['settings'] == {'log_bin': 'mysql-bin', 'expire_logs_days': '2'}  # 10.3: no binlog_expire_logs_seconds
    fmt = items['mysql.binlog_format']
    assert fmt['downtime'] == 'restart' and fmt['action'] == 'dropin' and not fmt['sql']  # folded: no SET GLOBAL
    g = items['mysql.grants']
    assert g['needs_admin'] and g['sql'] == ["GRANT REPLICATION SLAVE, REPLICATION CLIENT, RELOAD ON *.* TO "
                                              "'app'@'localhost'"]
    cs = items['mysql.client_charset']
    assert cs['auto'] and cs['files'] == ['/etc/mysql/mariadb.conf.d/50-client.cnf'] and cs['downtime'] == 'none'
    plan = pe.build_plan({'id': 'j', 'name': 'J'}, 'mysql', t, list(items.values()),
                         ['mysql.log_bin', 'mysql.binlog_format', 'mysql.grants'])
    assert plan['restart']['settings'] == {'binlog_format': 'ROW', 'expire_logs_days': '2', 'log_bin': 'mysql-bin'}
    assert plan['admin'] and 'mariadb.service' in plan['downtime']
    assert any(s.startswith('Restart: validate') for s in plan['steps'])
    # Without the restart the format change is SET GLOBAL + drop-in (no downtime)
    items2 = {i['id']: i for i in pe.mysql_items(b, t, MARIA_HOST, {}, restart_selected=False)}
    assert items2['mysql.binlog_format']['action'] == 'dropin_sql'
    assert items2['mysql.binlog_format']['sql'] == ["SET GLOBAL binlog_format = 'ROW'"]
    assert items2['mysql.binlog_format']['downtime'] == 'none'


def test_mysql8_dynamic_settings_use_set_persist():
    from app import pitr
    facts = {'version': '8.4.3', 'user': 'app@%', 'log_bin': '1', 'binlog_format': 'MIXED',
             'binlog_row_image': 'MINIMAL', 'server_id': '1', 'binlog_expire_seconds': '2592000', 'gtid_mode': 'ON',
             'grants': 'GRANT REPLICATION SLAVE, REPLICATION CLIENT, RELOAD ON *.* TO `app`@`%`', 'uid': '0',
             'systemd': '1', 'binlog_client': 'mysqlbinlog', 'binlog_client_version': 'mysqlbinlog  Ver 8.4.3 for Linux',
             'dump_client': 'mysqldump', 'dump_client_version': 'mysqldump  Ver 8.4.3 for Linux', 'free_mb': '9000'}
    checks = pitr.evaluate_mysql_prereqs(facts)
    host = dict(MARIA_HOST, unit='mysql.service', charset_files='')
    b = bundle(facts, checks, host)
    t = pe.mysql_target(DB, SERVER, b, host)
    items = {i['id']: i for i in pe.mysql_items(b, t, host, {})}
    assert items['mysql.binlog_format']['sql'] == ["SET PERSIST binlog_format = 'ROW'"]
    assert items['mysql.binlog_row_image']['sql'] == ["SET PERSIST binlog_row_image = 'FULL'"]
    assert all(i['downtime'] == 'none' for i in items.values())
    assert items['mysql.binlog_format']['needs_admin']  # no SUPER / SYSTEM_VARIABLES_ADMIN
    plan = pe.build_plan({'id': 'j'}, 'mysql', t, list(items.values()), pe.default_selection(list(items.values())))
    assert plan['restart'] is None and plan['downtime'] == 'None'


def test_managed_database_items_are_manual_and_restart_refused():
    facts, checks = maria_checks()
    b = bundle(facts, checks, MARIA_HOST)
    t = pe.mysql_target(dict(DB, host='x.db.ondigitalocean.com'), SERVER, b, MARIA_HOST)
    items = pe.mysql_items(b, t, MARIA_HOST, {})
    by = {i['id']: i for i in items}
    assert not by['mysql.log_bin']['auto'] and by['mysql.log_bin']['action'] == 'dropin'
    assert 'DigitalOcean' in by['mysql.log_bin']['manual']['steps'][0]
    assert by['mysql.binlog_format']['action'] == 'manual'
    assert by['mysql.grants']['auto']  # SQL still works against a managed server
    assert 'mysql.log_bin' not in pe.default_selection(items)
    with pytest.raises(pe.PlanError) as e:
        pe.build_plan({'id': 'j'}, 'mysql', t, items, ['mysql.log_bin'])
    assert e.value.code == 'invalid_items'


def test_docker_without_conf_mount_gets_compose_snippet():
    facts, checks = maria_checks()
    host = dict(MARIA_HOST, container_found='1', docker_mounts='/var/lib/mysql|/d|true|volume;')
    b = bundle(facts, checks, host)
    t = pe.mysql_target(dict(DB, docker_container='koha-db'), SERVER, b, host)
    by = {i['id']: i for i in pe.mysql_items(b, t, host, {})}
    lb = by['mysql.log_bin']
    assert not lb['auto'] and any('--log-bin=mysql-bin' in c for c in lb['manual']['commands'])
    assert by['mysql.binlog_format']['action'] == 'sql'  # SET GLOBAL now, config by hand


def test_pg_items_split_reload_and_restart():
    from app import pitr
    facts = {'server_version_num': '100023', 'server_version': '10.23', 'wal_level': 'replica',
             'archive_mode': 'off', 'archive_command': '(disabled)', 'archive_timeout': '0', 'uid': '0',
             'glibc': '2.27'}
    checks = pitr.evaluate_pg_prereqs(facts)
    host = {'uid': '0', 'systemd': '1', 'unit': 'postgresql@10-main.service',
            'data_directory': '/var/lib/postgresql/10/main', 'config_file': '/etc/postgresql/10/main/postgresql.conf'}
    t = pe.pg_target({'name': 'DSpace'}, SERVER, facts, host)
    assert t['config_method'] == 'alter_system' and t['restart_supported']
    items = {i['id']: i for i in pe.pg_items({'checks': checks}, t, host)}
    assert items['pg.archive_mode']['action'] == 'alter_system_restart' and items['pg.archive_mode']['downtime'] == 'restart'
    assert items['pg.archive_command']['action'] == 'alter_system_reload'
    assert items['pg.archive_command']['sql'] == [f"ALTER SYSTEM SET archive_command = '{pitr.WALG_ARCHIVE_COMMAND}'"]
    assert items['pg.archive_timeout']['downtime'] == 'none'
    assert items['pg.walg_install']['auto'] and 'pg.wal_level' not in items
    t2 = pe.pg_target({'name': 'DSpace'}, SERVER, facts, dict(host, allow_alter_system='off',
                                                             include_dir='/etc/postgresql/17/main/conf.d'))
    assert t2['config_method'] == 'conf_d' and t2['config_path'].endswith('/conf.d/zz-backupx-pitr.conf')


def test_restart_section_is_stable_when_no_downtime_items_are_done():
    facts, checks = maria_checks()
    b = bundle(facts, checks, MARIA_HOST)
    t = pe.mysql_target(DB, SERVER, b, MARIA_HOST)
    items = pe.mysql_items(b, t, MARIA_HOST, {})
    at = datetime(2030, 1, 1, 1, 30, tzinfo=timezone.utc)
    p1 = pe.build_plan({'id': 'j'}, 'mysql', t, items, ['mysql.log_bin', 'mysql.binlog_format', 'mysql.grants'],
                       'at', at)
    # grants applied: the item is gone, the restart part is the same
    items2 = [i for i in items if i['id'] != 'mysql.grants']
    p2 = pe.build_plan({'id': 'j'}, 'mysql', t, items2, ['mysql.log_bin', 'mysql.binlog_format'], 'at', at)
    from app import security
    assert security.plan_hash(p1['restart']) == security.plan_hash(p2['restart'])
    assert security.plan_hash(p1) != security.plan_hash(p2)
    with pytest.raises(pe.PlanError):
        pe.build_plan({'id': 'j'}, 'mysql', t, items, ['mysql.log_bin'], 'at', None)


def test_restart_at_validation_and_fresh_backup():
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    assert pe.parse_restart_at('2026-11-02T01:30:45Z', now).isoformat() == '2026-11-02T01:30:00+00:00'
    for bad in ('nope', '2026-10-01T00:00:00Z', '2027-06-01T00:00:00Z', None):
        with pytest.raises(pe.PlanError):
            pe.parse_restart_at(bad, now)
    assert pe.fresh_backup_decision((now - timedelta(minutes=20)).isoformat(), now) == 'skip'
    assert pe.fresh_backup_decision((now - timedelta(hours=2)).isoformat(), now) == 'run'
    assert pe.fresh_backup_decision(None, now) == 'run'
    assert pe.fresh_backup_decision((now - timedelta(minutes=5)).isoformat(), now, force=True) == 'run'


def test_dropin_merge_and_option_override_detection():
    content = pe.dropin_content('[mysqld]\nbinlog_format = ROW\n', {'log_bin': 'mysql-bin'})
    assert pe.parse_cnf(content)['mysqld'] == {'binlog_format': 'ROW', 'log_bin': 'mysql-bin'}
    eff = pe.effective_options(['--log_bin=mysql-bin', '--binlog-format=ROW', '--skip-log-bin'])
    assert pe.overridden_settings(eff, {'log_bin': 'mysql-bin', 'binlog_format': 'ROW'}) == [('log_bin', 'OFF')]
    eff = pe.effective_options(['--skip-log-bin', '--log_bin=mysql-bin', '--expire_logs_days=2'])
    assert pe.overridden_settings(eff, {'log_bin': 'mysql-bin', 'expire_logs_days': '2'}) == []
    assert 'default-character-set = utf8mb4' in pe.charset_dropin('utf8mb4')


def test_health_decision_never_crashes_while_recovering():
    cfg = {'health_timeout': 60, 'recovery_max_wait': 600}
    want = {'log_bin': 'mysql-bin'}
    ok = {'state': 'active', 'connected': True, 'values': {'log_bin': '1'}}
    assert pe.health_decision('mysql', ok, want, 5, cfg)[0] == pe.HEALTHY
    assert pe.health_decision('mysql', dict(ok, values={'log_bin': '0'}), want, 5, cfg)[0] == pe.UNHEALTHY
    assert pe.health_decision('mysql', {'state': 'failed'}, want, 5, cfg)[0] == pe.CRASHED
    assert pe.health_decision('mysql', {'state': 'activating', 'sub': 'auto-restart'}, want, 5, cfg)[0] == pe.CRASHED
    rec = {'state': 'activating', 'log': ['InnoDB: Starting crash recovery from checkpoint LSN=1']}
    assert pe.health_decision('mysql', rec, want, 30, cfg)[0] == pe.WAIT
    assert pe.health_decision('mysql', rec, want, 300, cfg)[0] == pe.WAIT   # past the health timeout: still waits
    assert pe.health_decision('mysql', rec, want, 700, cfg)[0] == pe.RECOVERY_TIMEOUT
    # crashed state but the log says recovery: not a crash
    assert pe.health_decision('mysql', dict(rec, state='failed'), want, 30, cfg)[0] == pe.WAIT
    assert pe.health_decision('postgres', {'state': 'activating', 'log': ['database system is starting up']},
                              {}, 300, cfg)[0] == pe.WAIT
    # the old instance before the restart began
    assert pe.health_decision('mysql', dict(ok, fresh=False), want, 2, cfg)[0] == pe.WAIT
    assert pe.health_decision('mysql', {'state': 'activating'}, want, 30, cfg)[0] == pe.WAIT
    assert pe.health_decision('mysql', {'state': 'active', 'connected': False}, want, 120, cfg)[0] == pe.UNHEALTHY


def test_phase_of_step():
    assert pe.phase_of_step('Restart: restarting mariadb.service') == 'restart'
    assert pe.phase_of_step('Anchor: waiting') == 'anchor'
    assert pe.phase_of_step('Done') is None


# =============================================================================
# Credentials and scripts
# =============================================================================

def test_credentials_are_taken_once_and_expire():
    credentials.put('t1', {'mode': 'password', 'username': 'root', 'password': ADMIN_PW})
    assert credentials.has('t1')
    assert credentials.take('t1')['password'] == ADMIN_PW
    assert credentials.take('t1') is None
    credentials.put('t2', {'mode': 'socket'}, seconds=-1)
    assert credentials.take('t2') is None
    assert credentials.validate_admin({'mode': 'password', 'username': 'root'})[1]
    assert credentials.validate_admin({'mode': 'socket'}) == ({'mode': 'socket'}, None)
    assert credentials.validate_admin({'mode': 'x'})[1]


def test_admin_password_only_in_the_option_file_content():
    script = ms.mysql_sql_script(DB, ["GRANT RELOAD ON *.* TO 'app'@'%'"], '',
                                 {'mode': 'password', 'username': 'root', 'password': ADMIN_PW})
    start, end = script.index('CNF_CONTENT='), script.index('unset CNF_CONTENT')
    block = script[start:end]
    assert ADMIN_PW in block and 'user="root"' in block and '> "$CONF_DIR/my.cnf"' in block
    assert ADMIN_PW not in script[:start] + script[end:]
    assert "trap 'rm -rf \"$CONF_DIR\"' EXIT" in script and 'umask 077' in script
    assert '-p' + ADMIN_PW not in script
    sock = ms.mysql_sql_script(DB, ['SELECT 1'], '', {'mode': 'socket'})
    assert DB_PW not in sock and '--protocol=socket' in sock


def test_pinned_tasks_only_claimed_by_their_worker(db):
    row, _ = tasks.enqueue('pitr_enable', {'job_id': 'pxjob'}, pinned_worker='worker-a')
    assert tasks.claim_one('worker-b') is None
    got = tasks.claim_one('worker-a')
    assert got and got['id'] == row['id']
    db.execute("UPDATE tasks SET status = 'failed' WHERE id = %s", (row['id'],))
    db.commit()
    old, _ = tasks.enqueue('pitr_enable', {'job_id': 'pxjob2'}, pinned_worker='gone')
    db.execute('UPDATE tasks SET created_at = %s WHERE id = %s',
               (tasks.utcnow() - timedelta(hours=1), old['id']))
    db.commit()
    assert tasks.fail_orphaned_pinned() == 1
    assert 'credential' in tasks.get_task(old['id'])['error']


# =============================================================================
# Service + API (SSH faked)
# =============================================================================

@pytest.fixture
def db():
    import app.main  # noqa: F401
    from app.db.factory import get_database
    database = get_database()

    def clean():
        database.rollback()
        for sql in ("DELETE FROM task_logs", "DELETE FROM tasks", "DELETE FROM health_probes WHERE key LIKE 'pitrplan:px%%'",
                    "DELETE FROM pitr_state WHERE job_id LIKE 'px%%'", "DELETE FROM jobs WHERE id LIKE 'px%%'",
                    "DELETE FROM servers WHERE id LIKE 'px%%'", "DELETE FROM db_configs WHERE id LIKE 'px%%'",
                    "DELETE FROM s3_configs WHERE id LIKE 'px%%'"):
            database.execute(sql)
        database.commit()
    clean()
    yield database
    clean()


@pytest.fixture
def world(db, monkeypatch):
    import app.main as main
    main.create_server({'id': 'pxsrv', 'name': 'koha-host', 'host': 'bxhost', 'ssh_port': 22, 'ssh_user': 'root',
                        'ssh_auth_type': 'password', 'ssh_password': 'Ssh-Secret-pw1'})
    main.create_s3_config({'id': 'pxs1', 'name': 'PX', 'endpoint': 's3.example.com', 'bucket': 'bk',
                           'access_key': 'AK-px', 'secret_key': 'SK-px-secret'})
    main.create_db_config({'id': 'pxdb', 'name': 'Koha DB', 'type': 'mysql', 'host': '127.0.0.1', 'port': 3306,
                           'username': 'app', 'password': DB_PW, 'databases': 'app'})
    main.save_job('pxjob', {'name': 'Koha', 'backup_type': 'database', 'server_id': 'pxsrv',
                            'database_config_id': 'pxdb', 's3_config_id': 'pxs1', 's3_config_ids': ['pxs1'],
                            'restic_password': 'Restic-Secret-pw1', 'backup_prefix': 'pxjob'})
    monkeypatch.setattr(main.limiter, 'enabled', False)
    return main


class FakeSSH:
    """Answers maintenance scripts by what they contain; records every script (stdin) it got."""

    def __init__(self, statuses=None, valid=True, pd=('--log_bin=mysql-bin', '--binlog_format=ROW',
                                                         '--expire_logs_days=2')):
        self.scripts = []
        self.statuses = list(statuses or [])
        self.valid = valid
        self.pd = pd
        self.status_calls = 0

    def kinds(self):
        return [k for k, _s in self.scripts]

    def __call__(self, server, script, timeout=120):
        import subprocess
        out = ''
        if 'validate-config' in script or '--help --verbose' in script:
            kind = 'validate'
            out = ''.join(f'MAINT\tpd\t{x}\n' for x in self.pd)
            out += 'MAINT\tvalid\t1\n' if self.valid else "MAINT\tvalid\t0\nMAINT\tverr\tmysqld: unknown variable 'bogus=1'\n"
        elif 'systemctl restart --no-block' in script or 'docker restart' in script:
            kind = 'restart'
            out = f'MAINT\tsince\t{int(_time.time())}\nMAINT\trestarted\t1\n'
        elif 'ActiveState' in script or '.State.Status' in script:
            kind = 'status'
            self.status_calls += 1
            st = self.statuses.pop(0) if len(self.statuses) > 1 else (self.statuses[0] if self.statuses else
                                                                      {'state': 'active', 'connected': '1',
                                                                       'var_log_bin': '1', 'var_binlog_format': 'ROW',
                                                                       'var_expire_logs_days': '2'})
            out = ''.join(f'MAINT\t{k}\t{v}\n' for k, v in st.items() if k != 'log')
            out += ''.join(f'MAINT\tlog\t{x}\n' for x in st.get('log', []))
        elif 'tar -czf' in script:
            kind = 'backup_config'
            out = 'MAINT\tbackup\t/var/lib/backupx-pitr/config-backups/x/config.tgz\n'
        elif 'fact restored' in script:
            kind = 'restore_file'
            out = 'MAINT\trestored\t/etc/mysql/mariadb.conf.d/zz-backupx-pitr.cnf\n'
        elif '.backupx-new' in script:
            kind = 'write_file'
            out = 'MAINT\twrote\tx\n'
        elif 'SQL_0=' in script:
            kind = 'sql'
            n = script.count('\nSQL_')
            out = ''.join(f'MAINT\tsql\t{i} ok\n' for i in range(n))
        elif 'moved by backupx' in script:
            kind = 'charset'
            out = 'MAINT\tok\t1\n'
        else:
            kind = 'other'
        self.scripts.append((kind, script))
        return subprocess.CompletedProcess([], 0, out, '')


@pytest.fixture
def fake(world, monkeypatch):
    from app import pitr_enable_service as svc
    facts, checks = maria_checks()
    state = {'bundle': bundle(facts, checks, MARIA_HOST)}

    def gather(job_id):
        b = dict(state['bundle'], checked_at=datetime.now(timezone.utc).isoformat())
        from app import health_service
        health_service.save_probe(svc.PROBE_PREFIX + job_id, b)
        return b
    monkeypatch.setattr(svc, 'gather', gather)
    ssh = FakeSSH()
    monkeypatch.setattr(svc, '_run', ssh)
    sent = []
    monkeypatch.setattr(world, 'send_notification', lambda *a, **k: sent.append(a))
    monkeypatch.setattr(svc, 'phase_enable', lambda ctx, job_id: {'state': 'ok', 'window_start': 'a',
                                                                  'window_end': 'b'})
    backups = []
    monkeypatch.setattr(world, 'run_backup', lambda job_id, **k: backups.append(k) or (True, 'Backup completed'))
    monkeypatch.setenv('PITR_MAINT_POLL_INTERVAL', '1')
    return {'svc': svc, 'ssh': ssh, 'sent': sent, 'state': state, 'backups': backups}


@pytest.fixture
def api(client, world):
    assert client.post('/api/auth/login', json={'username': 'testadmin', 'password': 'testpassword123'}).status_code == 200
    return client


@pytest.fixture
def runner(db):
    import app.main as main
    rr = tasks.TaskRunner(main._lock_connection, max_workers=1, worker_id='test-p3')
    yield rr
    rr.stop(wait=True)


def _plan(api, **body):
    res = api.post('/api/jobs/pxjob/pitr/plan', json=body)
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def test_plan_endpoint_and_apply_gates(api, fake):
    plan = _plan(api)
    ids = [i['id'] for i in plan['items']]
    assert 'mysql.log_bin' in ids and plan['needs_restart'] and plan['confirm_text'] == 'Koha DB'
    assert plan['admin']['needed'] and plan['admin']['socket_available']
    sel = plan['selection']['items']
    body = {'items': sel, 'restart': 'now', 'plan_hash': plan['plan_hash'], 'confirm': 'Koha DB',
            'admin': {'mode': 'password', 'username': 'root', 'password': ADMIN_PW}}
    res = api.post('/api/jobs/pxjob/pitr/apply', json=body)
    assert res.status_code == 403 and res.get_json()['code'] == 'step_up_required'
    assert api.post('/api/auth/step-up', json={'password': 'testpassword123'}).status_code == 200
    assert api.post('/api/jobs/pxjob/pitr/apply', json=dict(body, confirm='nope')).get_json()['code'] == \
        'confirmation_mismatch'
    assert api.post('/api/jobs/pxjob/pitr/apply', json=dict(body, plan_hash=None)).get_json()['code'] == \
        'plan_hash_missing'
    res = api.post('/api/jobs/pxjob/pitr/apply', json=dict(body, plan_hash='0' * 64))
    assert res.status_code == 409 and res.get_json()['code'] == 'plan_changed' and res.get_json()['plan_hash']
    res = api.post('/api/jobs/pxjob/pitr/apply', json=dict(body, admin=None))
    assert res.status_code == 400 and res.get_json()['code'] == 'admin_required'
    res = api.post('/api/jobs/pxjob/pitr/apply', json=body)
    assert res.status_code == 202, res.get_json()
    task = res.get_json()['task']
    assert task['kind'] == 'pitr_enable' and task['tier'] == 'maintenance'
    row = tasks.get_task(task['id'])
    assert ADMIN_PW not in json.dumps(dict(row), default=str)
    assert credentials.has(task['id'])
    res = api.post('/api/jobs/pxjob/pitr/apply', json=body)
    assert res.status_code == 409 and res.get_json()['code'] == 'busy'
    credentials.discard(task['id'])


def _start(api, fake, **extra):
    plan = _plan(api, **{k: v for k, v in extra.items() if k in ('items', 'restart', 'restart_at')})
    assert api.post('/api/auth/step-up', json={'password': 'testpassword123'}).status_code == 200
    body = dict({'items': plan['selection']['items'], 'restart': 'now', 'plan_hash': plan['plan_hash'],
                 'confirm': 'Koha DB', 'admin': {'mode': 'password', 'username': 'root', 'password': ADMIN_PW}},
                **extra)
    body['plan_hash'] = plan['plan_hash']
    res = api.post('/api/jobs/pxjob/pitr/apply', json=body)
    assert res.status_code == 202, res.get_json()
    return res.get_json()['task']


def test_apply_now_full_flow_and_secrets(api, fake, runner, db):
    plan = _plan(api)
    task = _start(api, fake, items=plan['selection']['items'] + ['mysql.client_charset'])
    assert runner.run_once() == 'succeeded', tasks.get_task(task['id'])['error']
    kinds = fake['ssh'].kinds()
    # charset fix, grants (admin); then config backup, drop-in, validation, restart, health
    assert kinds.index('charset') < kinds.index('sql') < kinds.index('backup_config') < kinds.index('write_file') \
        < kinds.index('validate') < kinds.index('restart') and kinds[-1] == 'status'
    sql_scripts = [s for k, s in fake['ssh'].scripts if k == 'sql']
    assert any(ADMIN_PW in s for s in sql_scripts)  # only on ssh stdin
    drop = next(s for k, s in fake['ssh'].scripts if k == 'write_file')
    assert 'log_bin = mysql-bin' in drop and 'binlog_format = ROW' in drop
    assert fake['backups'] and fake['backups'][0]['tags'] == ['automated', 'pre-maintenance']
    row = tasks.get_task(task['id'])
    logs = '\n'.join(line['line'] for line in tasks.get_logs(task['id'], limit=5000))
    audit = db.fetchall("SELECT * FROM audit_log WHERE action = 'PITR_ENABLE'")
    for blob in (json.dumps(dict(row), default=str), logs, json.dumps([dict(a) for a in audit], default=str)):
        assert ADMIN_PW not in blob and DB_PW not in blob
    assert not credentials.has(task['id'])
    run = fake['svc'].current_run('pxjob')
    assert run['phase'] == 'done' and run['result']['state'] == 'ok'


def test_validation_failure_blocks_restart_and_undoes(api, fake, runner):
    fake['ssh'].valid = False
    task = _start(api, fake, items=['mysql.log_bin'])
    assert runner.run_once() == 'failed'
    kinds = fake['ssh'].kinds()
    assert 'restart' not in kinds and 'restore_file' in kinds
    assert 'did not validate' in tasks.get_task(task['id'])['error']


def test_overridden_setting_blocks_restart(api, fake, runner):
    fake['ssh'].pd = ('--log_bin=mysql-bin', '--skip-log-bin', '--expire_logs_days=2')
    task = _start(api, fake, items=['mysql.log_bin'])
    assert runner.run_once() == 'failed'
    assert 'restart' not in fake['ssh'].kinds()
    assert 'overridden' in tasks.get_task(task['id'])['error']


def test_crash_after_restart_rolls_back_and_alerts(api, fake, runner):
    fake['ssh'].statuses = [{'state': 'active', 'restart_count': '0'},       # baseline before the restart
                            {'state': 'failed', 'connected': '0', 'log': ['[ERROR] mysqld: File not found']},
                            {'state': 'active', 'connected': '1', 'var_log_bin': '0'}]  # after rollback
    task = _start(api, fake, items=['mysql.log_bin'])
    assert runner.run_once() == 'rolled_back'
    kinds = fake['ssh'].kinds()
    assert kinds.count('restart') == 2 and 'restore_file' in kinds
    assert kinds.index('restore_file') > kinds.index('restart')
    assert fake['sent'] and 'restored' in fake['sent'][0][3]
    assert tasks.get_task(task['id'])['status'] == 'rolled_back'


def test_crash_recovery_is_never_rolled_back(api, fake, runner, monkeypatch):
    from app import pitr_enable_service as svc

    class Clock:
        t = 0.0

        def monotonic(self):
            return self.t

        def sleep(self, s):
            self.t += 400

        def time(self):
            return _time.time()

        def strftime(self, *a):
            return _time.strftime(*a)

        def gmtime(self, *a):
            return _time.gmtime(*a)
    monkeypatch.setattr(svc, 'time', Clock())
    monkeypatch.setenv('PITR_MAINT_HEALTH_TIMEOUT', '300')
    monkeypatch.setenv('PITR_MAINT_RECOVERY_MAX_WAIT', '1000')
    fake['ssh'].statuses = [{'state': 'active', 'restart_count': '0'},
                            {'state': 'activating', 'connected': '0',
                             'log': ['InnoDB: Starting crash recovery from checkpoint LSN=123']}]
    task = _start(api, fake, items=['mysql.log_bin'])
    assert runner.run_once() == 'failed'
    kinds = fake['ssh'].kinds()
    assert 'restore_file' not in kinds and kinds.count('restart') == 1
    assert 'did not roll back' in tasks.get_task(task['id'])['error'] and fake['sent']


def test_scheduled_window_runs_at_not_before(api, fake, runner, db):
    at = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(second=0, microsecond=0)
    task = _start(api, fake, restart='at', restart_at=at.isoformat())
    assert runner.run_once() == 'succeeded'
    parent = tasks.get_task(task['id'])
    result = json.loads(parent['result'])
    child_id = result['scheduled_restart_task_id']
    assert 'restart' not in fake['ssh'].kinds()          # only the no-downtime items ran
    assert fake['svc'].current_run('pxjob')['phase'] == 'scheduled'
    assert runner.run_once() is None                     # not before the window
    child = tasks.get_task(child_id)
    assert child['status'] == 'queued' and child['pinned_worker'] is None
    # The window arrives; the no-downtime items are done (grants/charset gone from the check)
    facts, checks = maria_checks(reload=True, charset=False)
    facts['grants'] = 'GRANT REPLICATION SLAVE, REPLICATION CLIENT, RELOAD ON *.* TO `app`@`localhost`'
    from app import pitr
    fake['state']['bundle'] = bundle(facts, pitr.evaluate_mysql_prereqs(facts), MARIA_HOST)
    db.execute('UPDATE tasks SET not_before = %s WHERE id = %s', (tasks.utcnow() - timedelta(seconds=1), child_id))
    db.commit()
    assert runner.run_once() == 'succeeded', tasks.get_task(child_id)['error']
    assert 'restart' in fake['ssh'].kinds()
    assert fake['svc'].current_run('pxjob')['phase'] == 'done'


def test_window_refuses_a_changed_restart_plan(api, fake, runner, db, monkeypatch):
    at = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(second=0, microsecond=0)
    task = _start(api, fake, restart='at', restart_at=at.isoformat())
    assert runner.run_once() == 'succeeded'
    child_id = json.loads(tasks.get_task(task['id'])['result'])['scheduled_restart_task_id']
    monkeypatch.setenv('PITR_BINLOG_BASENAME', 'other-bin')  # the restart would write something else now
    db.execute('UPDATE tasks SET not_before = %s WHERE id = %s', (tasks.utcnow() - timedelta(seconds=1), child_id))
    db.commit()
    n = len(fake['ssh'].scripts)
    assert runner.run_once() == 'failed'
    assert 'restart' not in fake['ssh'].kinds()[n:] and 'write_file' not in fake['ssh'].kinds()[n:]


def test_task_refuses_plan_changed_after_confirm(api, fake, runner, monkeypatch):
    task = _start(api, fake, items=['mysql.log_bin'])
    monkeypatch.setenv('PITR_BINLOG_BASENAME', 'changed-bin')
    assert runner.run_once() == 'failed'
    assert fake['ssh'].kinds() == [] and 'plan changed' in tasks.get_task(task['id'])['error']
    assert not credentials.has(task['id'])


def test_lost_credential_fails_cleanly(api, fake, runner):
    task = _start(api, fake)
    credentials.discard(task['id'])
    assert runner.run_once() == 'failed'
    assert 'admin login is gone' in tasks.get_task(task['id'])['error'] and fake['ssh'].kinds() == []


def test_pg_reload_then_restart_and_rollback_via_reset(world, fake, runner, monkeypatch, api, db):
    from app import pitr
    world.create_db_config({'id': 'pxpg', 'name': 'DSpace', 'type': 'postgres', 'host': 'localhost', 'port': 5432,
                            'username': 'postgres', 'password': 'Pg-Secret-1', 'databases': 'dspace'})
    job = world.get_job('pxjob')
    job['database_config_id'] = 'pxpg'
    world.save_job('pxjob', job)
    facts = {'server_version_num': '100023', 'server_version': '10.23', 'wal_level': 'replica',
             'archive_mode': 'off', 'archive_command': '(disabled)', 'archive_timeout': '900', 'uid': '0',
             'glibc': '2.27', 'walg_version': 'wal-g version v2.0.1'}
    host = {'uid': '0', 'systemd': '1', 'unit': 'postgresql@10-main.service', 'data_directory': '/var/lib/pg',
            'config_file': '/etc/postgresql/10/main/postgresql.conf', 'postgres_bin': '/usr/lib/postgresql/10/bin/postgres',
            'auto_conf_b64': ms.b64("# Do not edit\narchive_timeout = '900'\n")}
    fake['state']['bundle'] = dict(bundle(facts, pitr.evaluate_pg_prereqs(facts), host, kind='postgres'),
                                   facts=facts)
    monkeypatch.setenv('BACKUPX_TEST_FAULT', 'verify')
    fake['ssh'].statuses = [{'state': 'active'},
                            {'state': 'active', 'connected': '1', 'var_archive_mode': 'on'},
                            {'state': 'active', 'connected': '1', 'var_archive_mode': 'off'}]
    plan = _plan(api)
    ids = {i['id']: i for i in plan['items']}
    assert ids['pg.archive_command']['downtime'] == 'none' and ids['pg.archive_mode']['downtime'] == 'restart'
    assert api.post('/api/auth/step-up', json={'password': 'testpassword123'}).status_code == 200
    res = api.post('/api/jobs/pxjob/pitr/apply', json={'items': plan['selection']['items'], 'restart': 'now',
                                                        'plan_hash': plan['plan_hash'], 'confirm': 'DSpace'})
    assert res.status_code == 202, res.get_json()
    assert runner.run_once() == 'rolled_back'
    sqls = [s for k, s in fake['ssh'].scripts if k == 'sql']
    joined = '\n'.join(sqls)
    assert 'ALTER SYSTEM SET archive_command' in joined and 'pg_reload_conf' in joined
    assert 'ALTER SYSTEM SET archive_mode' in joined and 'ALTER SYSTEM RESET archive_mode' in joined
    kinds = fake['ssh'].kinds()
    assert kinds.count('restart') == 2


def test_enable_run_endpoint(api, fake):
    res = api.get('/api/jobs/pxjob/pitr/enable')
    assert res.status_code == 200 and res.get_json()['run'] is None


# =============================================================================
# Health hooks
# =============================================================================

def test_health_issues_point_to_the_wizard():
    from app import health
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = {'now': now, 'jobs': {'j1': {'name': 'Koha', 'pitr_enabled': True, 'backup_type': 'database'}},
             'pitr': {'j1': {'kind': 'mysql', 'state': 'pending'}},
             'probes': {'prereq:j1': {'data': {'ok': False, 'failing': ['Binary log (log_bin)'],
                                               'warnings': ['Privilege RELOAD']}}}}
    issues = {i.kind: i for i in health.detect_pitr(state)}
    assert issues['pitr_log_bin_off'].wizard == {'kind': 'pitr_enable', 'job_id': 'j1'}
    assert issues['pitr_reload_missing'].to_dict()['wizard']['job_id'] == 'j1'
    state['pitr']['j1'] = {'kind': 'postgres', 'state': 'pending', 'last_tick_at': now, 'streamer_active': 'inactive'}
    state['probes'] = {}
    issues = {i.kind: i for i in health.detect_pitr(state)}
    assert issues['wal_archiving_off'].wizard and issues['wal_archiving_off'].manual['commands'] == []


def test_every_maintenance_script_is_valid_bash():
    """Render each script with representative inputs and run `bash -n` on it."""
    import shutil
    import subprocess
    if not shutil.which('bash'):
        pytest.skip('no bash')
    host_t = {'install': 'host', 'flavor': 'mariadb', 'version': '10.3.39', 'service': 'mariadb.service',
              'container': None, 'config_path': '/etc/mysql/mariadb.conf.d/zz-backupx-pitr.cnf'}
    dock_t = dict(host_t, install='docker', container='db', service=None, config_path='/srv/conf/zz-backupx-pitr.cnf')
    scripts = [
        ms.mysql_host_probe_script(DB), ms.mysql_host_probe_script(dict(DB, docker_container='db')),
        ms.mysql_sql_script(DB, ['SELECT 1', "GRANT RELOAD ON *.* TO 'a'@'%'"], '', None, query='SELECT 1'),
        ms.mysql_sql_script(DB, ['SELECT 1'], 'percona/percona-server:8.4',
                            {'mode': 'password', 'username': 'r', 'password': ADMIN_PW}),
        ms.mysql_sql_script(DB, ['SELECT 1'], '', {'mode': 'socket'}),
        ms.mysql_backup_config_script('t', host_t), ms.mysql_backup_config_script('t', dock_t),
        ms.write_file_script('t', '/etc/x.cnf', "[mysqld]\nlog_bin = x\n"), ms.restore_file_script('t', '/etc/x.cnf'),
        ms.mysql_validate_script(host_t), ms.mysql_validate_script(dict(host_t, flavor='mysql', version='8.4.3')),
        ms.mysql_validate_script(dock_t), ms.mysql_restart_script(host_t), ms.mysql_restart_script(dock_t),
        ms.mysql_status_script(host_t, DB, '', ['log_bin'], 1, '/var/log/mysql/error.log'),
        ms.mysql_status_script(dock_t, dict(DB, docker_container='db'), '', ['log_bin'], 1),
        ms.charset_fix_script('t', ['/etc/mysql/a.cnf'], 'utf8mb4', '/etc/mysql/conf.d'),
        ms.pg_host_probe_script(), ms.pg_sql_script(["ALTER SYSTEM SET a = 'b'"], query='SELECT 1'),
        ms.pg_backup_config_script('t', '/var/lib/pg', '/etc/pg/postgresql.conf'),
        ms.pg_validate_script('/usr/lib/postgresql/10/bin/postgres', '/etc/pg/postgresql.conf', ['archive_mode']),
        ms.pg_restart_script('postgresql@10-main.service'), ms.pg_status_script('postgresql@10-main.service',
                                                                                  ['archive_mode'], 1, '/var/log/p.log'),
    ]
    for script in scripts:
        res = subprocess.run(['bash', '-n'], input=script, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr + script[:400]

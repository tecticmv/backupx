"""
Production safety guarantees (docs/PITR-VERIFICATION.md), enforced on the scripts backupx sends:

(a) backup / streaming / prerequisite / health / verify paths only read: every SQL statement
    they send to a production database is SELECT / SHOW (or SET SESSION), never DDL/DML or a
    server-state change; mysqldump always uses --single-transaction; the only lock is the brief
    FLUSH TABLES WITH READ LOCK that mysqldump --source-data/--master-data takes for the anchor.
(b) the streamer only reads binlogs: mysqlbinlog --read-from-remote-server with its own
    connection server id (no CHANGE MASTER / START REPLICA, nothing configured on the source).
(c) restores import into production only from the gated replace/undo flow.
(d) the maintenance flow (config change + restart) is gated: tests/test_pitr_enable.py.

The end-to-end rig additionally records every statement backupx's login sends (general_log) and
checks it against the same allow-list.
"""
import ast
import inspect
import re

from app import maint_scripts as ms, pitr, pitr_scripts as ps, restore as r, restore_scripts as rs

DB = {'name': 'DB', 'host': '10.0.0.5', 'port': 3306, 'username': 'app', 'password': 'Db-Secret-1'}
DB_DOCKER = dict(DB, docker_container='koha-db')

ALLOWED = re.compile(r'^\s*(SELECT|SHOW|SET\s+SESSION)\b', re.I)
FORBIDDEN = re.compile(
    r'\b(INSERT|UPDATE|DELETE|REPLACE\s+INTO|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|RENAME|'
    r'SET\s+(GLOBAL|PERSIST)|FLUSH|RESET\s+(MASTER|SLAVE|REPLICA|BINARY)|PURGE\s+(BINARY|MASTER)|'
    r'LOCK\s+TABLES|KILL|CHANGE\s+(MASTER|REPLICATION)|START\s+(SLAVE|REPLICA)|STOP\s+(SLAVE|REPLICA)|'
    r'INSTALL\s+PLUGIN|SHUTDOWN)\b')

SQL_SITES = (
    re.compile(r'pitr_q\s+"([^"]+)"'),
    re.compile(r"pitr_q\s+'([^']+)'"),
    re.compile(r'-e\s+"([^"$]+)"', re.S),
    re.compile(r"-e\s+'([^']+)'"),
    re.compile(r"run_sql\s+\S+\s+\S+\s+'((?:[^']|'\"'\"')+)'"),
)


def statements(script):
    out = []
    for site in SQL_SITES:
        for m in site.finditer(script):
            body = m.group(1).replace("'\"'\"'", "'")
            out += [s.strip() for s in body.split(';') if s.strip()]
    return out


def _read_only(script, what):
    sql = statements(script)
    bad = [s for s in sql if not ALLOWED.match(s)]
    assert not bad, f'{what}: non-read statements {bad}'
    code = '\n'.join(line for line in script.splitlines() if not line.lstrip().startswith('#'))
    hit = FORBIDDEN.search(code)
    assert not hit, f'{what}: forbidden SQL keyword {hit.group(0)!r} in {code[max(0, hit.start() - 80):hit.end() + 40]!r}'
    return sql


def test_prereq_scripts_only_read():
    for cfg in (DB, DB_DOCKER):
        for image in ('', 'percona/percona-server:8.4'):
            sql = _read_only(ps.mysql_prereq_script(cfg, image), 'prereq')
            assert any(s.startswith('SHOW GRANTS') for s in sql) and any('@@log_bin' in s for s in sql)
    _read_only(ps.pg_prereq_script(), 'pg prereq')


def test_streamer_only_reads_binlogs():
    sql = _read_only(ps.STREAMER_SCRIPT, 'streamer')
    assert set(s.split()[0].upper() for s in sql) <= {'SELECT', 'SHOW'}
    assert '--read-from-remote-server --raw --stop-never "$SID_OPT"' in ps.STREAMER_SCRIPT
    assert 'connection-server-id=$PITR_SERVER_ID' in ps.STREAMER_SCRIPT
    # a unique replica id per job, far above real server ids
    ids = {pitr.connection_server_id(f'job{i}') for i in range(200)}
    assert len(ids) == 200 and min(ids) >= 3_000_000_000
    for cfg in (DB, DB_DOCKER):
        _read_only(ps.ensure_and_scan_script('j', cfg, ''), 'ensure+scan')


def test_maintenance_status_and_probe_scripts_only_read():
    host_t = {'install': 'host', 'flavor': 'mysql', 'version': '8.4.3', 'service': 'mysql.service',
              'container': None, 'config_path': '/etc/mysql/mysql.conf.d/zz-backupx-pitr.cnf'}
    _read_only(ms.mysql_status_script(host_t, DB, '', ['log_bin', 'binlog_format'], 1), 'status')
    _read_only(ms.mysql_host_probe_script(DB), 'host probe')
    _read_only(ms.pg_host_probe_script(), 'pg host probe')


def test_verify_against_production_only_reads():
    for engine in (r.ENGINE_MYSQL, r.ENGINE_MARIADB):
        _read_only(rs.verify_body(engine, ['app'], ['app.t'], '', analyze=False), 'verify (prod)')
    body = rs.verify_body(r.ENGINE_POSTGRES, ['dspace'], ['dspace.public.item'], '', analyze=False)
    assert 'ANALYZE' not in body  # ANALYZE only on the scratch copy
    _read_only(body, 'verify pg (prod)')


def test_backup_dump_is_a_consistent_read(monkeypatch):
    """The nightly mysqldump: --single-transaction; the only lock is the anchor's brief FTWRL."""
    import app.main as main
    src = inspect.getsource(main.run_database_backup)
    assert '--single-transaction' in src
    for flag in ('--lock-all-tables', '--flush-logs', '--delete-master-logs', '--flush-privileges', '--lock-tables'):
        assert flag not in src
    assert "echo --source-data=2 || echo --master-data=2" in src  # =2: commented position, no CHANGE MASTER run


def test_restores_import_into_production_only_in_replace_and_rollback():
    tree = ast.parse(inspect.getsource(rs))
    users = set()
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        text = ast.get_source_segment(inspect.getsource(rs), fn) or ''
        if re.search(r'\|\s*bximport|\bbximport\s+"', text):
            users.add(fn.name)
    assert users <= {'replace_script', 'rollback_script'}, users
    from app import restore_service as svc
    source = inspect.getsource(svc)
    callers = {m.group(1) for m in re.finditer(r'def (\w+)\([^)]*\):(?:(?!\ndef ).)*?\bprod_sql\(', source, re.S)}
    assert callers <= {'prod_sql', 'run_verify'}, callers
    calls = {m.group(1) for m in re.finditer(r'def (\w+)\([^)]*\):(?:(?!\ndef ).)*?\bdo_replace\(', source, re.S)}
    assert calls <= {'do_replace', '_task_replace', '_task_undo'}, calls


def test_client_containers_are_locked_down():
    assert '--read-only' in ps.CLIENT_RUNNER and '--cap-drop ALL' in ps.CLIENT_RUNNER
    assert 'no-new-privileges' in ps.CLIENT_RUNNER and ' -p ' not in ps.CLIENT_RUNNER
    runner = rs.prod_runner(DB, r.ENGINE_MYSQL, 'percona/percona-server:8.4')
    assert '--read-only' in runner
    # the password only appears in the printf builtin (no argv) writing the 0600 option file
    start = runner.index('( umask 077; printf')
    end = runner.index('> "$BXD/my.cnf"', start)
    assert DB['password'] in runner[start:end]
    assert DB['password'] not in runner[:start] + runner[end:]


def test_auto_client_image_resolution():
    assert pitr.resolve_client_image('8.4.13')['image'] == 'percona/percona-server:8.4'
    assert pitr.resolve_client_image('8.0.40-0ubuntu0.22.04.1')['image'] == 'percona/percona-server:8.0'
    assert pitr.resolve_client_image('10.6.22-MariaDB-0ubuntu0.22.04.1')['image'] == 'mariadb:10.6'
    res = pitr.resolve_client_image('10.9.3-MariaDB')
    assert res['image'] == 'mariadb:10.11' and 'nearest newer' in res['warning']
    assert pitr.effective_client_image({'pitr_client_auto': True, 'pitr_client_image': 'x',
                                        'pitr_client_image_digest': 'mariadb@sha256:ab'}) == 'mariadb@sha256:ab'
    assert pitr.effective_client_image({'pitr_client_auto': False, 'pitr_client_image': 'percona/percona-server:8.4',
                                        'pitr_client_image_digest': 'y'}) == 'percona/percona-server:8.4'
    assert pitr.effective_client_image({}) == ''  # existing jobs: host binaries, unchanged


def test_server_version_change_starts_a_new_chain():
    state = {'chain_id': 1, 'server_uuid': 'u1', 'binlog_base': 'mysql-bin', 'last_seq': 4}
    scan = {'files': [{'name': 'mysql-bin.000005', 'size': 10, 'mtime': 1},
                      {'name': 'mysql-bin.000006', 'size': 10, 'mtime': 2}], 'uuid': 'u1', 'events': []}
    plan = pitr.plan_shipment(state, scan, version_change='server version changed (mysql 8.0 -> mysql 8.4)')
    assert plan['breaks'] and plan['breaks'][0]['reason'].startswith('server version changed')
    assert plan['chain_id'] == 2
    same = pitr.plan_shipment(state, scan)
    assert not same['breaks']
    assert pitr.version_key('8.0.40') == 'mysql 8.0' and pitr.version_key('10.6.1-MariaDB') == 'mariadb 10.6'


def test_managed_connection_error_explains_trusted_sources():
    checks = pitr.evaluate_mysql_prereqs({'connect_error': "ERROR 2003 (HY000): Can't connect to MySQL server on "
                                                          "'db.ondigitalocean.com' (110)"})
    assert 'trusted sources' in checks[0]['fix']

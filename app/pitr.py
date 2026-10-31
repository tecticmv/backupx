"""
Point-in-time recovery (PITR): pure helpers, no database, no subprocess.

MySQL / MariaDB
---------------
A small streamer runs on the job's SSH server as a systemd unit
(backupx-pitr-binlog@<job>.service). It keeps `mysqlbinlog
--read-from-remote-server --raw --stop-never` connected to the database and
writes byte-exact copies of the binary logs into a spool directory. Every few
minutes backupx ships the closed files (all but the one being written) with
restic to a sibling repository `<prefix>-binlog` on each of the job's storages,
plus a copy of the open file ("partial") so the restorable window doesn't wait
for the server to rotate. Shipped files are deleted from the spool only after
every storage has them.

The nightly mysqldump is the anchor: with PITR on it adds --source-data=2
(--master-data=2 on MariaDB / older MySQL), so the dump header records the
binlog file and position of its snapshot. A restore loads the dump, then
replays the binlogs from that position up to the target time.

Chains: binlog files are numbered. Shipped files must continue the sequence
(same server, same base name); anything else (files purged on the server before
they were streamed, a server_uuid change after a failover, a hole in the spool)
breaks the chain. A broken chain is not restorable past the break until a new
anchor (dump) lands inside the new chain. Times before the break stay
restorable from the old chain.

PostgreSQL uses WAL-G instead (see the WAL-G helpers at the end); this module
only holds what can be checked without a server.
"""

import os
import re
from datetime import datetime, timedelta, timezone

PITR_SCHEDULE_SUFFIX = '__pitr'
WALG_BASE_SCHEDULE_SUFFIX = '__walg_base'
BINLOG_REPO_SUFFIX = '-binlog'
WAL_REPO_SUFFIX = '-wal'

# States shown in the UI. Alerts go out when a job enters a bad state and when
# it gets back to ok (see alert_decision).
STATE_DISABLED = 'disabled'
STATE_PENDING = 'pending'        # enabled, first shipment not done yet
STATE_OK = 'ok'
STATE_LAGGING = 'lagging'        # anchored, but the newest restorable time is too old
STATE_BROKEN = 'broken'          # the chain broke and no anchor has landed in the new one
STATE_UNANCHORED = 'unanchored'  # streaming fine, but no dump anchors this chain yet
BAD_STATES = (STATE_LAGGING, STATE_BROKEN, STATE_UNANCHORED)

DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_RETENTION_DAYS = 7


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def settings():
    """PITR settings from the environment, read on every call."""
    return {
        # Threads of the APScheduler 'pitr' executor (shipping never waits for a backup slot)
        'max_concurrent': max(1, _env_int('PITR_MAX_CONCURRENT', 2)),
        # Minutes the newest restorable time may trail "now" before the job is lagging;
        # 0 = automatic (3 x interval, at least 15 minutes)
        'lag_minutes': _env_int('PITR_LAG_MINUTES', 0),
        'ship_timeout': _env_int('PITR_SHIP_TIMEOUT', 900),
        # The streamer's last "in sync" heartbeat may be this old and still count as live
        'heartbeat_max_age': _env_int('PITR_HEARTBEAT_MAX_AGE', 300),
    }


def lag_threshold_minutes(interval_minutes, configured=0, kind='mysql'):
    if configured and configured > 0:
        return configured
    interval = max(1, int(interval_minutes or DEFAULT_INTERVAL_MINUTES))
    if kind == 'postgres':
        # archive_timeout (15 min) bounds how long an idle server keeps a WAL segment open
        return max(15 + 2 * interval, 25)
    return max(3 * interval, 15)


def job_kind(db_config):
    """'postgres' or 'mysql' for a job's database config (None if unknown)."""
    if not db_config:
        return None
    return 'postgres' if (db_config.get('type') or 'mysql') in ('postgres', 'postgresql') else 'mysql'


def pitr_settings_from_request(data, current=None):
    """Validate the PITR fields of a job create/update request.

    Returns (fields, error). Missing fields keep the current values.
    """
    current = current or {}
    fields = {
        'pitr_enabled': bool(data.get('pitr_enabled', current.get('pitr_enabled', False))),
        'pitr_interval': data.get('pitr_interval', current.get('pitr_interval') or DEFAULT_INTERVAL_MINUTES),
        'pitr_retention_days': data.get('pitr_retention_days',
                                        current.get('pitr_retention_days') or DEFAULT_RETENTION_DAYS),
        'pitr_client_image': (data.get('pitr_client_image', current.get('pitr_client_image')) or '').strip(),
    }
    try:
        fields['pitr_interval'] = int(fields['pitr_interval'])
        fields['pitr_retention_days'] = int(fields['pitr_retention_days'])
    except (TypeError, ValueError):
        return None, 'PITR interval and retention must be whole numbers'
    if not 1 <= fields['pitr_interval'] <= 60:
        return None, 'PITR interval must be between 1 and 60 minutes'
    if not 1 <= fields['pitr_retention_days'] <= 365:
        return None, 'PITR retention must be between 1 and 365 days'
    image = fields['pitr_client_image']
    if image and not re.match(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$', image):
        return None, 'PITR client image must be a docker image reference such as mysql:8.4'
    return fields, None


def connection_server_id(job_id):
    """A replica server_id for the streamer's connection, stable per job.

    Must differ from the source's server_id and from any other replica; real
    servers use small numbers, so this stays in a high range.
    """
    import zlib
    return 3_000_000_000 + zlib.crc32(job_id.encode('utf-8')) % 1_000_000_000


# ---------------------------------------------------------------------------
# Binlog names
# ---------------------------------------------------------------------------

_BINLOG_NAME = re.compile(r'^([A-Za-z0-9_.-]+)\.(\d{6,})$')


def split_binlog_name(name):
    """'mysql-bin.000123' -> ('mysql-bin', 123); None if it isn't a binlog file name."""
    match = _BINLOG_NAME.match(name or '')
    if not match:
        return None
    return match.group(1), int(match.group(2))


def binlog_seq(name):
    parts = split_binlog_name(name)
    return parts[1] if parts else None


# ---------------------------------------------------------------------------
# Dump anchor
# ---------------------------------------------------------------------------

_CHANGE_SOURCE = re.compile(
    r"CHANGE\s+(?:MASTER|REPLICATION\s+SOURCE)\s+TO\b.*?"
    r"(?:MASTER|SOURCE)_LOG_FILE\s*=\s*'([^']+)'\s*,\s*(?:MASTER|SOURCE)_LOG_POS\s*=\s*(\d+)",
    re.IGNORECASE | re.DOTALL)
_GTID_PURGED = re.compile(r"GTID_PURGED\s*=\s*(?:/\*!\d+\s*'\+'\s*\*/\s*)?'([^']*)'", re.IGNORECASE | re.DOTALL)
_MARIADB_GTID = re.compile(r"gtid_slave_pos\s*=\s*'([^']*)'", re.IGNORECASE)


def parse_dump_anchor(text):
    """Binlog position recorded in a mysqldump header.

    Understands MySQL >= 8.0.26 (CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE=...),
    older MySQL and MariaDB (CHANGE MASTER TO MASTER_LOG_FILE=...), commented
    (--source-data=2) or not. Returns {'file', 'pos', 'seq', 'gtid_set'} or None.
    """
    text = text or ''
    match = _CHANGE_SOURCE.search(text)
    if not match:
        return None
    name, pos = match.group(1), int(match.group(2))
    if not split_binlog_name(name):
        return None
    gtid = _GTID_PURGED.search(text) or _MARIADB_GTID.search(text)
    gtid_set = re.sub(r'\s+', '', gtid.group(1)) if gtid else ''
    return {'file': name, 'pos': pos, 'seq': binlog_seq(name), 'gtid_set': gtid_set}


ANCHOR_LINE_PREFIX = 'backupx-anchor:'
ANCHOR_TIME_PREFIX = 'backupx-anchor-time:'
ANCHOR_FAILED_PREFIX = 'backupx-anchor-failed:'


def extract_anchor_lines(lines):
    """Split the dump script's anchor marker lines out of its plain output.

    Returns (anchor_or_None, anchor_error_or_None, remaining_lines).
    """
    header, rest, started, error = [], [], None, None
    for line in lines or []:
        if line.startswith(ANCHOR_LINE_PREFIX):
            header.append(line[len(ANCHOR_LINE_PREFIX):].strip())
        elif line.startswith(ANCHOR_TIME_PREFIX):
            try:
                started = int(line[len(ANCHOR_TIME_PREFIX):].strip())
            except ValueError:
                pass
        elif line.startswith(ANCHOR_FAILED_PREFIX):
            error = (error + ' ' if error else '') + line[len(ANCHOR_FAILED_PREFIX):].strip()
        else:
            rest.append(line)
    anchor = parse_dump_anchor('\n'.join(header)) if header else None
    if anchor is not None and started is not None:
        anchor['time'] = datetime.fromtimestamp(started, timezone.utc)
    if header and anchor is None and error is None:
        error = 'the dump header has no binlog position'
    return anchor, error, rest


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

def _check(name, status, current, required, fix='', needs_restart=False):
    return {'check': name, 'status': status, 'current': current, 'required': required,
            'fix': fix, 'needs_restart': needs_restart}


def _on(value):
    return str(value or '').strip().upper() in ('ON', '1', 'YES', 'TRUE')


def _version_tuple(text):
    match = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text or '')
    if not match:
        return None
    return tuple(int(x or 0) for x in match.groups())


def server_flavor(version_text):
    return 'mariadb' if 'mariadb' in (version_text or '').lower() else 'mysql'


def suggested_client_image(version_text):
    """Image with a binlog client matching the server.

    The official mysql images ship no mysqlbinlog; Percona Server's images do
    (same code base, same binlog format) and exist for every MySQL 8.x line.
    MariaDB images include mariadb-binlog.
    """
    version = _version_tuple(version_text)
    if not version:
        return None
    if server_flavor(version_text) == 'mariadb':
        return f"mariadb:{version[0]}.{version[1]}"
    return f"percona/percona-server:{version[0]}.{version[1]}"


def scratch_server_image(version_text):
    """Image for the throwaway restore server: same flavor and major.minor as production."""
    version = _version_tuple(version_text)
    if not version:
        return 'mysql:8.4'
    return f"{server_flavor(version_text)}:{version[0]}.{version[1]}"


def _grants_have(grants, privilege):
    text = (grants or '').upper()
    return 'ALL PRIVILEGES ON *.*' in text or bool(re.search(r'\b' + privilege + r'\b', text))


def evaluate_mysql_prereqs(facts, mode='direct', client_image=''):
    """Turn what the prerequisite script found into a table of checks.

    facts: {key: value} from the remote script (see pitr_scripts.mysql_prereq_script).
    Each row: check, status (ok / warn / fail), current, required, fix, needs_restart.
    """
    checks = []
    if facts.get('connect_error') or not facts.get('version'):
        checks.append(_check('Database connection', 'fail', facts.get('connect_error') or 'no answer',
                             'the streamer host can log in to the database',
                             'Check host/port/credentials and that this server is a trusted source of the database.'))
        _append_host_checks(checks, facts, mode, client_image)
        return checks

    version = facts.get('version', '')
    flavor = server_flavor(version)
    checks.append(_check('Server', 'ok', version, 'MySQL 5.7+ / MariaDB 10.3+'))

    log_bin = facts.get('log_bin', '')
    checks.append(_check(
        'Binary log (log_bin)', 'ok' if _on(log_bin) else 'fail', log_bin or 'unknown', 'ON',
        '' if _on(log_bin) else
        ('Add to the [mysqld] section: log_bin=mysql-bin, server_id=1, binlog_format=ROW, '
         'expire_logs_days=2 (MariaDB: binlog_expire_logs_seconds=172800 on 10.6+); then restart the database.'),
        needs_restart=not _on(log_bin)))

    fmt = (facts.get('binlog_format') or '').upper()
    checks.append(_check(
        'binlog_format', 'ok' if fmt == 'ROW' else ('warn' if fmt == 'MIXED' else 'fail'), fmt or 'unknown', 'ROW',
        '' if fmt == 'ROW' else 'SET PERSIST binlog_format=ROW (MySQL) or binlog_format=ROW in my.cnf + '
                                'SET GLOBAL binlog_format=ROW (MariaDB). Statement-based logs replay unreliably.'))

    row_image = (facts.get('binlog_row_image') or '').upper()
    if row_image:
        checks.append(_check('binlog_row_image', 'ok' if row_image == 'FULL' else 'warn', row_image, 'FULL',
                             '' if row_image == 'FULL' else 'Set binlog_row_image=FULL.'))

    server_id = facts.get('server_id', '')
    sid_ok = str(server_id).strip() not in ('', '0')
    checks.append(_check('server_id', 'ok' if sid_ok else 'fail', server_id or 'unknown', 'non-zero',
                         '' if sid_ok else 'Set server_id=1 in my.cnf and restart.', needs_restart=not sid_ok))

    gtid = facts.get('gtid_mode', '')
    if flavor == 'mysql':
        checks.append(_check('gtid_mode', 'ok' if _on(gtid) else 'warn', gtid or 'OFF', 'ON (recommended)',
                             '' if _on(gtid) else 'Optional. Restores use file/position, GTID only adds a cross-check.'))

    expire = facts.get('binlog_expire_seconds', '')
    try:
        expire_s = int(float(expire))
    except (TypeError, ValueError):
        expire_s = None
    if expire_s is not None:
        status = 'ok' if expire_s == 0 or expire_s >= 6 * 3600 else 'warn'
        checks.append(_check(
            'Binlog retention on the server', status,
            'never purged' if expire_s == 0 else f'{expire_s // 3600}h {expire_s % 3600 // 60}m',
            '6h+ (time the streamer may be down without a gap)',
            '' if status == 'ok' else
            'Short server-side retention: a streamer outage longer than this breaks the chain until the '
            'next dump. Managed services may not allow changing it.'))

    grants = facts.get('grants', '')
    for privilege, why in (('REPLICATION SLAVE', 'stream binary logs'),
                           ('REPLICATION CLIENT', 'list binary logs (SHOW BINARY LOGS)')):
        alt = 'REPLICATION REPLICA' if privilege == 'REPLICATION SLAVE' else 'BINLOG MONITOR'
        ok = _grants_have(grants, privilege) or _grants_have(grants, alt)
        checks.append(_check(f'Privilege {privilege}', 'ok' if ok else 'fail', 'granted' if ok else 'missing',
                             f'needed to {why}',
                             '' if ok else f"GRANT {privilege} ON *.* TO '{facts.get('user', 'user')}'@'%';"))
    reload_ok = _grants_have(grants, 'RELOAD') or _grants_have(grants, 'BACKUP_ADMIN')
    checks.append(_check('Privilege RELOAD', 'ok' if reload_ok else 'warn', 'granted' if reload_ok else 'missing',
                         'consistent dump position (--source-data)',
                         '' if reload_ok else
                         "GRANT RELOAD ON *.* TO the backup user. Without it the nightly dump can't record "
                         "its binlog position (it falls back to a plain dump and PITR stays unanchored)."))

    _append_host_checks(checks, facts, mode, client_image, server_version=version)
    return checks


def _append_host_checks(checks, facts, mode, client_image, server_version=''):
    is_root = facts.get('is_root') == '1'
    sudo = facts.get('sudo') == '1'
    checks.append(_check('Root or passwordless sudo', 'ok' if (is_root or sudo) else 'fail',
                         'root' if is_root else ('sudo -n works' if sudo else 'neither'),
                         'install the systemd unit and /etc/backupx-pitr',
                         '' if (is_root or sudo) else 'Use root for this server or allow passwordless sudo.'))
    systemd = facts.get('systemd') == '1'
    checks.append(_check('systemd', 'ok' if systemd else 'fail', 'yes' if systemd else 'no', 'systemd host',
                         '' if systemd else 'The streamer runs as a systemd unit.'))

    uses_docker = mode == 'docker' or bool(client_image)
    if uses_docker:
        docker = facts.get('docker') == '1'
        checks.append(_check('Docker', 'ok' if docker else 'fail', 'available' if docker else 'not available',
                             'run the client from ' + (client_image or "the database container's image"),
                             '' if docker else 'Install docker, or clear the client image to use local binaries.'))
    client = facts.get('binlog_client', '')
    client_version = facts.get('binlog_client_version', '')
    if not client:
        suggestion = suggested_client_image(server_version)
        checks.append(_check('mysqlbinlog', 'fail', 'not found', 'mysqlbinlog / mariadb-binlog',
                             'Install the client matching the server' +
                             (f', or set the client image to {suggestion}' if suggestion else '') + '.'))
    elif server_version:
        server_flav = server_flavor(server_version)
        dump_version = facts.get('dump_client_version', '')
        client_flav = _client_flavor(f'{client} {client_version}', dump_version)
        # Older MariaDB binlog clients don't print the server release; the
        # mysqldump from the same install (host or client image) does.
        client_release = _client_server_version(client_version)
        if client_release is None and _client_flavor(dump_version) == client_flav:
            client_release = _client_server_version(dump_version)
        sv, cv = _version_tuple(server_version), _version_tuple(client_release)
        suggestion = suggested_client_image(server_version)
        if server_flav != client_flav:
            checks.append(_check('mysqlbinlog matches the server', 'fail', f'{client} ({client_version.strip()})',
                                 f'{server_flav} client',
                                 f'MySQL and MariaDB binlog clients are not compatible. Set the client image to {suggestion}.'))
        elif sv and cv and cv[:2] < sv[:2]:
            checks.append(_check('mysqlbinlog matches the server', 'warn', client_version.strip(),
                                 f'{sv[0]}.{sv[1]} or newer',
                                 f'Older client than server; set the client image to {suggestion} to be safe.'))
        else:
            checks.append(_check('mysqlbinlog matches the server', 'ok', client_version.strip(), f'{server_flav} client'))

    if server_version:
        checks.append(_dump_client_check(facts, server_version))

    free_mb = facts.get('free_mb', '')
    try:
        free = int(free_mb)
    except (TypeError, ValueError):
        free = None
    if free is not None:
        checks.append(_check('Free space for the spool', 'ok' if free >= 2048 else 'warn', f'{free} MB',
                             '2 GB+ under /var/lib', '' if free >= 2048 else 'Binlogs queue here if shipping stalls.'))


def _dump_client_check(facts, server_version):
    """The anchor dump's mysqldump must speak the server's dialect to record the position."""
    client = facts.get('dump_client', '')
    version_text = facts.get('dump_client_version', '')
    suggestion = suggested_client_image(server_version)
    name = 'mysqldump can record the binlog position'
    if not client:
        return _check(name, 'fail', 'not found', 'mysqldump / mariadb-dump',
                      f'Install the matching client or set the client image to {suggestion}.')
    server_flav = server_flavor(server_version)
    client_flav = _client_flavor(f'{client} {version_text}')
    sv = _version_tuple(server_version)
    cv = _version_tuple(_client_server_version(version_text))
    current = version_text.strip() or client
    if server_flav == 'mysql' and sv and sv[:2] >= (8, 2) and (client_flav != 'mysql' or (cv and cv[:2] < (8, 2))):
        return _check(name, 'fail', current, 'MySQL 8.2+ client',
                      'MySQL 8.2+ removed SHOW MASTER STATUS, which older and MariaDB clients send: the '
                      'dump would fall back to one without a position (PITR unanchored). '
                      f'Set the client image to {suggestion} (needs docker) or install the MySQL '
                      f'{sv[0]}.{sv[1]} client (mysql-community-client from repo.mysql.com).')
    if server_flav != client_flav:
        return _check(name, 'warn', current, f'{server_flav} client',
                      f'Mixing MySQL and MariaDB clients is fragile; set the client image to {suggestion}.')
    return _check(name, 'ok', current, f'{server_flav} client')


def _client_server_version(text):
    """Server release a mysql/mariadb client was built from, or None if it doesn't say.

    Client tools carry their own version next to the server's, and only the
    server's is comparable with the database:
      'mysqldump  Ver 10.19 Distrib 10.6.22-MariaDB, for debian-linux-gnu (x86_64)' -> 10.6.22
      'mariadb-binlog from 11.4.13-MariaDB, client 3.5 for debian-linux-gnu (x86_64)' -> 11.4.13
      'mysqlbinlog  Ver 8.4.13 for Linux on x86_64 (MySQL Community Server - GPL)' -> 8.4.13
      'mariadb-binlog Ver 3.5 for debian-linux-gnu at x86_64' -> None (3.5 is the tool's version)
    """
    text = text or ''
    for pattern in (r'\bDistrib\s+(\d+\.\d+(?:\.\d+)?)', r'\bfrom\s+(\d+\.\d+(?:\.\d+)?)'):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    # MySQL 8.0+ prints the server release as the program version. Tool
    # versions (mysqlbinlog 3.x, mysqldump 10.x) are never MySQL majors 5-9.
    match = re.search(r'\bVer\s+(\d+)\.(\d+)(?:\.(\d+))?', text)
    if match and 5 <= int(match.group(1)) <= 9:
        return '.'.join(g for g in match.groups() if g is not None)
    return None


def _client_flavor(text, fallback_text=''):
    """'mariadb' or 'mysql' for a client --version line (plus its path).

    Older MariaDB mysqlbinlog ('mysqlbinlog Ver 3.5 for debian-linux-gnu at
    x86_64') names neither; then the other client from the same install decides.
    """
    if 'mariadb' in (text or '').lower():
        return 'mariadb'
    if _client_server_version(text) is None and 'mariadb' in (fallback_text or '').lower():
        return 'mariadb'
    return 'mysql'


def prereqs_ok(checks):
    return all(c['status'] != 'fail' for c in checks)


def parse_kv_output(text, prefix='PITR\t'):
    """Lines 'PITR\\tkey\\tvalue' -> {key: value} (repeated keys keep the last value)."""
    facts = {}
    for line in (text or '').splitlines():
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix):].split('\t', 1)
        facts[parts[0]] = parts[1] if len(parts) > 1 else ''
    return facts


# ---------------------------------------------------------------------------
# Spool scan -> what to ship
# ---------------------------------------------------------------------------

def parse_scan_output(text):
    """Read the ensure+scan script's output.

    Lines (tab separated, prefixed PITR): FILE name size mtime, UUID, LAST_SHIPPED,
    ACTIVE, CLOSED_ALL, HEARTBEAT, NOW, EVENT lineno text, JOURNAL text,
    SERVER_VERSION, INSTALL, ERROR.
    """
    scan = {'files': [], 'events': [], 'journal': [], 'uuid': '', 'last_shipped': '', 'active': '',
            'closed_all': False, 'heartbeat': None, 'now': None, 'server_version': '', 'install': '',
            'errors': []}
    for line in (text or '').splitlines():
        if not line.startswith('PITR\t'):
            continue
        parts = line.split('\t')
        key = parts[1] if len(parts) > 1 else ''
        values = parts[2:]
        if key == 'FILE' and len(values) >= 3:
            name = values[0]
            if split_binlog_name(name):
                try:
                    scan['files'].append({'name': name, 'size': int(values[1]), 'mtime': int(values[2])})
                except ValueError:
                    pass
        elif key == 'EVENT' and len(values) >= 2:
            try:
                text = '\t'.join(values[1:])
                stamp = re.match(r'^(\d{4}-\d\d-\d\dT\S+)\s+(.*)$', text)
                scan['events'].append({'line': int(values[0]), 'time': stamp.group(1) if stamp else '',
                                       'text': stamp.group(2) if stamp else text})
            except ValueError:
                pass
        elif key == 'JOURNAL':
            scan['journal'].append('\t'.join(values))
        elif key == 'ERROR':
            scan['errors'].append('\t'.join(values))
        elif key == 'UUID':
            scan['uuid'] = values[0] if values else ''
        elif key == 'LAST_SHIPPED':
            scan['last_shipped'] = values[0] if values else ''
        elif key == 'ACTIVE':
            scan['active'] = values[0] if values else ''
        elif key == 'CLOSED_ALL':
            scan['closed_all'] = bool(values) and values[0] == '1'
        elif key == 'HEARTBEAT':
            scan['heartbeat'] = _int_or_none(values[0] if values else '')
        elif key == 'NOW':
            scan['now'] = _int_or_none(values[0] if values else '')
        elif key == 'SERVER_VERSION':
            scan['server_version'] = values[0] if values else ''
        elif key == 'INSTALL':
            scan['install'] = values[0] if values else ''
    scan['files'].sort(key=lambda f: (split_binlog_name(f['name'])[0], binlog_seq(f['name'])))
    return scan


def _int_or_none(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _segment(files, chain_id):
    first, last = files[0], files[-1]
    return {
        'files': [f['name'] for f in files],
        'first_file': first['name'], 'last_file': last['name'],
        'first_seq': binlog_seq(first['name']), 'last_seq': binlog_seq(last['name']),
        'end_time': max(f['mtime'] for f in files),
        'size': sum(f['size'] for f in files),
        'chain_id': chain_id,
    }


def plan_shipment(state, scan):
    """Decide what to ship and whether the chain broke.

    state: the job's pitr_state row (dict; may be empty for a first run) with
      chain_id, server_uuid, binlog_base, last_seq, partial_file, partial_size, partial_mtime.
    scan: parse_scan_output result.

    Returns {
      'segments': [ {files, first_file, last_file, first_seq, last_seq, end_time, chain_id}, ...],
      'stale': [names already shipped (re-streamed copies), deleted without shipping],
      'partial': {name, size, mtime} or None,
      'breaks': [ {chain_id, start_seq, reason, server_uuid, base} ]  (new chains, in order),
      'chain_id', 'server_uuid', 'base', 'last_seq'  (state after this shipment),
      'ack_events': highest event line number handled,
    }
    """
    state = state or {}
    chain_id = int(state.get('chain_id') or 0)
    uuid = state.get('server_uuid') or ''
    base = state.get('binlog_base') or ''
    last_seq = state.get('last_seq')
    last_seq = int(last_seq) if last_seq is not None else None
    breaks = []

    def new_chain(start_seq, reason, new_base=None):
        nonlocal chain_id, last_seq
        chain_id += 1
        breaks.append({'chain_id': chain_id, 'start_seq': start_seq, 'reason': reason,
                       'server_uuid': scan.get('uuid') or uuid, 'base': new_base or base})
        last_seq = None if start_seq is None else start_seq - 1

    files = list(scan.get('files') or [])
    closed = files if scan.get('closed_all') else files[:-1]
    open_file = None if scan.get('closed_all') or not files else files[-1]

    # Streamer-reported gaps. "... continuing at <file>" pins the break to that file:
    # the numbering may have no hole (the file being streamed was purged and the
    # server went on with the next one) while data is still missing.
    break_points, generic_gap = {}, None
    for event in scan.get('events') or []:
        if not event['text'].startswith('gap'):
            continue
        reason = 'streamer: ' + event['text'][len('gap'):].strip(' :')
        at = re.search(r'continuing at (\S+)\s*$', event['text'])
        seq = binlog_seq(at.group(1)) if at else None
        if seq is not None:
            break_points[seq] = reason
        elif 'server_uuid changed' not in event['text']:
            generic_gap = reason
    ack = max((e['line'] for e in scan.get('events') or []), default=0)

    scan_uuid = scan.get('uuid') or ''
    if chain_id == 0:
        first = closed[0] if closed else open_file
        if first:
            base = split_binlog_name(first['name'])[0]
            uuid = scan_uuid
            new_chain(binlog_seq(first['name']), 'start', base)
    elif scan_uuid and uuid and scan_uuid != uuid:
        first = closed[0] if closed else open_file
        reason = f'server_uuid changed ({uuid} -> {scan_uuid})'
        uuid = scan_uuid
        new_chain(binlog_seq(first['name']) if first else None, reason,
                  split_binlog_name(first['name'])[0] if first else base)
    elif generic_gap and chain_id:
        first = closed[0] if closed else open_file
        new_chain(binlog_seq(first['name']) if first else None, generic_gap,
                  split_binlog_name(first['name'])[0] if first else base)
    if scan_uuid:
        uuid = scan_uuid

    segments, stale, run = [], [], []
    for f in closed:
        f_base, seq = split_binlog_name(f['name'])
        if base and f_base != base:
            if run:
                segments.append(_segment(run, chain_id))
                run = []
            base = f_base
            new_chain(seq, f'binlog base name changed to {f_base}', f_base)
        elif last_seq is not None and seq <= last_seq:
            stale.append(f['name'])  # already shipped in this chain (re-streamed after a restart)
            continue
        elif seq in break_points and not (breaks and breaks[-1]['start_seq'] == seq):
            if run:
                segments.append(_segment(run, chain_id))
                run = []
            new_chain(seq, break_points[seq])
        elif last_seq is not None and seq != last_seq + 1:
            if run:
                segments.append(_segment(run, chain_id))
                run = []
            missing = (f"{base}.{last_seq + 1:06d}" if seq == last_seq + 2
                       else f"{base}.{last_seq + 1:06d}..{base}.{seq - 1:06d}")
            new_chain(seq, f'missing binlog file(s) {missing}')
        elif last_seq is None and not breaks and chain_id:
            # Chain exists but nothing shipped yet in it (e.g. a break without a known start)
            pass
        run.append(f)
        last_seq = seq
    if run:
        segments.append(_segment(run, chain_id))

    partial = None
    if open_file:
        o_base, o_seq = split_binlog_name(open_file['name'])
        if o_seq in break_points and not (breaks and breaks[-1]['start_seq'] == o_seq) \
                and (last_seq is None or o_seq > last_seq):
            new_chain(o_seq, break_points[o_seq])
        elif last_seq is not None and o_base == base and o_seq != last_seq + 1 and o_seq > last_seq:
            # The open file doesn't follow what we shipped: report the hole now
            new_chain(o_seq, f"missing binlog file(s) before {open_file['name']}")
        changed = (open_file['name'] != state.get('partial_file')
                   or open_file['size'] != state.get('partial_size')
                   or open_file['mtime'] != state.get('partial_mtime'))
        if changed and open_file['size'] > 0:
            partial = dict(open_file)
    return {
        'segments': segments, 'stale': stale, 'partial': partial, 'open': open_file,
        'breaks': breaks, 'chain_id': chain_id, 'server_uuid': uuid, 'base': base,
        'last_seq': last_seq, 'ack_events': ack,
    }


# ---------------------------------------------------------------------------
# Restorable window, state, alerts
# ---------------------------------------------------------------------------

def _utc(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def anchor_usable(anchor, chain, min_retained_seq):
    """Can this dump start a replay inside this chain?"""
    if anchor.get('forgotten') or not anchor.get('snapshots'):
        return False
    if (anchor.get('server_uuid') or '') != (chain.get('server_uuid') or ''):
        return False
    a_base, a_seq = split_binlog_name(anchor.get('binlog_file') or '') or (None, None)
    if a_seq is None or a_base != chain.get('base'):
        return False
    start = chain.get('start_seq')
    if start is None or a_seq < start:
        return False
    return min_retained_seq is None or a_seq >= min_retained_seq


def chain_windows(chains, segments, anchors, partial=None):
    """Restorable windows per chain: [{chain_id, from, to, anchor_ids, reason}], oldest first.

    segments: shipped closed segments (dicts with chain_id, first_seq, last_seq,
      end_time, forgotten); partial: {chain_id, seq, until} for the newest open-file copy.
    A chain is restorable from its earliest usable anchor to the newest shipped data.
    """
    windows = []
    for chain in sorted(chains, key=lambda c: c['chain_id']):
        cid = chain['chain_id']
        segs = [s for s in segments if s['chain_id'] == cid]
        live = [s for s in segs if not s.get('forgotten')]
        forgotten = [s for s in segs if s.get('forgotten')]
        min_seq = (max(s['last_seq'] for s in forgotten) + 1) if forgotten else chain.get('start_seq')
        last_seq = max((s['last_seq'] for s in live), default=None)
        to = max((_utc(s['end_time']) for s in live), default=None)
        if partial and partial.get('chain_id') == cid:
            p_seq = partial.get('seq')
            if p_seq is not None and (last_seq is None or p_seq == last_seq + 1):
                to = max(filter(None, [to, _utc(partial.get('until'))]))
                last_seq = p_seq
        usable = [a for a in anchors if anchor_usable(a, chain, min_seq)
                  and last_seq is not None and binlog_seq(a['binlog_file']) <= last_seq]
        start = min((_utc(a['time']) for a in usable), default=None)
        windows.append({
            'chain_id': cid, 'reason': chain.get('reason') or '',
            'from': start if (start and to and start <= to) else None,
            'to': to, 'anchored': bool(usable and to and start <= to),
            'anchor_ids': [a.get('id') for a in usable],
        })
    return windows


def derive_state(enabled, has_run, windows, now, lag_minutes, streamer_live=True):
    """(state, window, lag_minutes) for the dashboard."""
    if not enabled:
        return STATE_DISABLED, None, None
    if not has_run or not windows:
        return STATE_PENDING, None, None
    current = windows[-1]
    if not current['anchored']:
        previous = next((w for w in reversed(windows[:-1]) if w['anchored']), None)
        state = STATE_UNANCHORED if current['reason'] == 'start' else STATE_BROKEN
        return state, previous, None
    lag = (_utc(now) - current['to']).total_seconds() / 60 if current['to'] else None
    if lag is None or lag > lag_minutes or not streamer_live:
        return STATE_LAGGING, current, lag
    return STATE_OK, current, lag


def alert_decision(new_state, alerted_state):
    """What to notify for a state change. Returns (kind or None, new alerted_state).

    kind is 'failed' when the job enters a bad state it hasn't alerted for yet
    (or a different bad state), 'recovered' when it is back to ok after an alert.
    Unanchored right after PITR is switched on is expected (the first dump
    hasn't run yet) and is not alerted; unanchored after an alert of another
    kind, or after ok, is.
    """
    if new_state == STATE_OK:
        return ('recovered' if alerted_state else None), None
    if new_state in BAD_STATES:
        if alerted_state == new_state:
            return None, alerted_state
        return 'failed', new_state
    return None, alerted_state  # pending / disabled: keep whatever was alerted


def should_alert_unanchored(state_since, now, grace_hours=26):
    """Unanchored for longer than a nightly cycle (+ slack) is worth an alert."""
    since = _utc(state_since)
    return since is not None and (_utc(now) - since) > timedelta(hours=grace_hours)


# ---------------------------------------------------------------------------
# Retention by anchor
# ---------------------------------------------------------------------------

def retention_plan(chains, segments, anchors, now, days):
    """Which shipped binlog segments can go.

    Keep everything back to the oldest usable anchor that is within `days`
    (its dump must still exist). Segments wholly before that anchor (older
    chains, or the same chain below the anchor's file) are forgotten. With no
    usable anchor inside the period nothing is forgotten: better too much than
    an unrestorable gap.

    Returns {'cutoff_anchor': anchor or None, 'forget': [segment, ...]}.
    """
    now = _utc(now)
    horizon = now - timedelta(days=max(1, int(days or DEFAULT_RETENTION_DAYS)))
    by_id = {c['chain_id']: c for c in chains}
    candidates = []
    for anchor in anchors:
        a_time = _utc(anchor.get('time'))
        if a_time is None or a_time < horizon:
            continue
        for chain in chains:
            if anchor_usable(anchor, chain, None):
                candidates.append((a_time, chain['chain_id'], anchor))
    if not candidates:
        return {'cutoff_anchor': None, 'forget': []}
    # Oldest anchor in the period; ties -> the older chain
    _, cutoff_chain, cutoff = min(candidates, key=lambda c: (c[0], c[1]))
    cutoff_seq = binlog_seq(cutoff['binlog_file'])
    forget = []
    for seg in segments:
        if seg.get('forgotten') or seg.get('kind') == 'partial':
            continue
        if seg['chain_id'] < cutoff_chain and seg['chain_id'] in by_id:
            forget.append(seg)
        elif seg['chain_id'] == cutoff_chain and seg['last_seq'] < cutoff_seq:
            forget.append(seg)
    return {'cutoff_anchor': cutoff, 'forget': forget}


# ---------------------------------------------------------------------------
# Restore planning
# ---------------------------------------------------------------------------

class RestorePlanError(ValueError):
    pass


def plan_restore(target_time, chains, segments, anchors, storage_id, partials=None):
    """Pick the dump and the binlog snapshots that restore to target_time on one storage.

    Returns {'anchor', 'segments' (ordered), 'partial' or None, 'chain_id'}.
    Raises RestorePlanError with a readable reason when the time isn't covered.
    """
    target = _utc(target_time)
    partials = partials or []
    for chain in sorted(chains, key=lambda c: -c['chain_id']):
        cid = chain['chain_id']
        live = sorted((s for s in segments if s['chain_id'] == cid and not s.get('forgotten')
                       and s.get('kind', 'closed') == 'closed' and (s.get('snapshots') or {}).get(storage_id)),
                      key=lambda s: s['first_seq'])
        forgotten = [s for s in segments if s['chain_id'] == cid and s.get('forgotten')]
        min_seq = (max(s['last_seq'] for s in forgotten) + 1) if forgotten else chain.get('start_seq')
        usable = [a for a in anchors if anchor_usable(a, chain, min_seq)
                  and (a.get('snapshots') or {}).get(storage_id) and _utc(a['time']) <= target]
        if not usable:
            continue
        anchor = max(usable, key=lambda a: _utc(a['time']))
        a_seq = binlog_seq(anchor['binlog_file'])
        needed, expect, covered_to = [], a_seq, None
        for seg in live:
            if seg['last_seq'] < a_seq:
                continue
            if seg['first_seq'] > expect:
                break  # hole (shouldn't happen inside a chain): stop here
            needed.append(seg)
            expect = seg['last_seq'] + 1
            covered_to = _utc(seg['end_time'])
            if covered_to >= target:
                break
        partial = None
        if covered_to is None or covered_to < target:
            cands = [p for p in partials if p['chain_id'] == cid and p['first_seq'] == expect
                     and (p.get('snapshots') or {}).get(storage_id)]
            if cands:
                partial = max(cands, key=lambda p: _utc(p['end_time']))
                covered_to = max(filter(None, [covered_to, _utc(partial.get('until') or partial['end_time'])]))
        if covered_to is None:
            if a_seq == expect and not needed and not partial:
                raise RestorePlanError('no binlogs shipped yet after the dump')
        if covered_to is None or covered_to < target:
            raise RestorePlanError(
                f"binlogs only reach {covered_to.isoformat() if covered_to else 'the dump'}; "
                f"target {target.isoformat()} is later")
        return {'anchor': anchor, 'segments': needed, 'partial': partial, 'chain_id': cid}
    raise RestorePlanError(f'no dump before {target.isoformat()} is usable for point-in-time restore '
                           'on this storage (PITR not anchored at that time)')


# ---------------------------------------------------------------------------
# WAL-G (PostgreSQL)
# ---------------------------------------------------------------------------

# WAL-G release used per glibc. Ubuntu 18.04 (glibc 2.27, e.g. the DSpace host
# with PostgreSQL 10) only has builds up to v2.0.1; v3 builds need glibc 2.31+
# and v3's CI no longer covers PostgreSQL 10.
WALG_BUILDS = (
    # (min glibc, version, asset, sha256 of the .tar.gz)
    ((2, 31), 'v3.0.9', 'wal-g-pg-20.04-amd64',
     '51ec330530f98fc3eb4f008fa539d291a2cac51e7076b256472862e45ceff0bd'),
    ((2, 27), 'v2.0.1', 'wal-g-pg-ubuntu-18.04-amd64',
     '21ee4ad9e1e874d1adf7f7fd3b1a79c3d1c6a754bc7d33c7478118fcdf1950d8'),
)


def walg_build_for(glibc_version, pg_major):
    """(version, asset, sha256) of the WAL-G build to install, or None."""
    glibc = _version_tuple(glibc_version)
    if not glibc:
        return None
    for min_glibc, version, asset, sha in WALG_BUILDS:
        if glibc[:2] >= min_glibc:
            if version.startswith('v3') and pg_major is not None and pg_major < 12:
                continue  # stay on v2.0.1 for old PostgreSQL even on a newer distro
            return version, asset, sha
    return None


WALG_ARCHIVE_COMMAND = '/usr/local/bin/wal-g-archive.sh %p'


def postgres_conf_changes(archive_timeout=900):
    """postgresql.conf lines the user applies in a restart window (never applied by backupx)."""
    return [
        "wal_level = replica            # PostgreSQL 10 default; 'minimal' can't archive",
        "archive_mode = on              # needs a restart",
        f"archive_command = '{WALG_ARCHIVE_COMMAND}'",
        f"archive_timeout = {int(archive_timeout)}          # seconds; bounds data loss on an idle server",
    ]


def evaluate_pg_prereqs(facts):
    checks = []
    if facts.get('connect_error') or not facts.get('server_version_num'):
        checks.append(_check('Database connection', 'fail', facts.get('connect_error') or 'no answer',
                             'psql as the postgres OS user on the server',
                             'WAL-G runs on the database host itself; the job must use that server.'))
        return checks
    try:
        num = int(facts['server_version_num'])
    except ValueError:
        num = 0
    major = num // 10000
    checks.append(_check('PostgreSQL', 'ok' if major >= 10 else 'fail', facts.get('server_version', str(num)),
                         '10+', '' if major >= 10 else 'WAL-G needs PostgreSQL 10 or newer here.'))
    wal_level = facts.get('wal_level', '')
    checks.append(_check('wal_level', 'ok' if wal_level in ('replica', 'logical') else 'fail', wal_level,
                         'replica', '' if wal_level in ('replica', 'logical') else 'wal_level = replica, restart',
                         needs_restart=wal_level not in ('replica', 'logical')))
    mode = facts.get('archive_mode', '')
    checks.append(_check('archive_mode', 'ok' if mode in ('on', 'always') else 'fail', mode or 'off', 'on',
                         '' if mode in ('on', 'always') else 'archive_mode = on in postgresql.conf, then restart '
                                                            '(schedule a window; backupx never restarts PostgreSQL).',
                         needs_restart=mode not in ('on', 'always')))
    command = facts.get('archive_command', '')
    ok = WALG_ARCHIVE_COMMAND.split()[0] in command
    checks.append(_check('archive_command', 'ok' if ok else 'fail', command or '(empty)', WALG_ARCHIVE_COMMAND,
                         '' if ok else f"archive_command = '{WALG_ARCHIVE_COMMAND}' then SELECT pg_reload_conf() "
                                       "(no restart needed for this one)."))
    timeout = facts.get('archive_timeout', '')
    try:
        t = int(re.sub(r'\D', '', timeout) or 0)
        if timeout.endswith('min'):
            t *= 60
    except ValueError:
        t = 0
    checks.append(_check('archive_timeout', 'ok' if 0 < t <= 900 else 'warn', timeout or '0', '900 (15 min)',
                         '' if 0 < t <= 900 else 'archive_timeout = 900 then SELECT pg_reload_conf().'))
    is_root = facts.get('is_root') == '1'
    sudo = facts.get('sudo') == '1'
    checks.append(_check('Root or passwordless sudo', 'ok' if (is_root or sudo) else 'fail',
                         'root' if is_root else ('sudo -n works' if sudo else 'neither'),
                         'install wal-g and /etc/wal-g', '' if (is_root or sudo) else 'Use root or passwordless sudo.'))
    build = walg_build_for(facts.get('glibc', ''), major or None)
    checks.append(_check('WAL-G build', 'ok' if build else 'fail', facts.get('glibc', 'unknown glibc'),
                         'glibc 2.27+ amd64', f'{build[0]} {build[1]}' if build else 'No WAL-G build for this host.'))
    installed = facts.get('walg_version', '')
    checks.append(_check('wal-g installed', 'ok' if installed else 'warn', installed or 'not yet',
                         build[0] if build else 'wal-g', '' if installed else 'Installed when PITR is enabled.'))
    failed = facts.get('archiver_failed_count', '')
    if failed not in ('', '0'):
        checks.append(_check('Archiver failures', 'warn', failed, '0',
                             f"Last failed WAL {facts.get('archiver_last_failed_wal', '?')} at "
                             f"{facts.get('archiver_last_failed_time', '?')}; check the PostgreSQL log."))
    return checks


# ---------------------------------------------------------------------------
# Ship script output
# ---------------------------------------------------------------------------

def parse_ship_output(text, target_count):
    """Per storage: {'exit_code', 'segments': {index: snapshot_id}, 'partial': snapshot_id, 'output': [lines]}.

    Also returns whether the script confirmed the spool cleanup (all storages ok).
    """
    import json
    results = [{'exit_code': None, 'segments': {}, 'partial': None, 'output': []} for _ in range(target_count)]
    current, what, shipped = None, None, False
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if line == 'PITR\tSHIPPED\t1':
            shipped = True
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError
        except ValueError:
            if current is not None and current < target_count:
                results[current]['output'].append(line)
            continue
        kind = msg.get('message_type')
        if kind == 'backupx_target':
            current, what = msg.get('index'), None
        elif kind == 'backupx_target_done':
            idx = msg.get('index')
            if isinstance(idx, int) and idx < target_count:
                results[idx]['exit_code'] = msg.get('exit_code')
        elif kind == 'backupx_pitr':
            what = (msg.get('what'), msg.get('index', 0))
        elif kind == 'summary' and current is not None and current < target_count and what:
            if what[0] == 'segment':
                results[current]['segments'][what[1]] = msg.get('snapshot_id')
            elif what[0] == 'partial':
                results[current]['partial'] = msg.get('snapshot_id')
        elif kind in ('error', 'exit_error') and current is not None and current < target_count:
            error = msg.get('message') or msg.get('error')
            if isinstance(error, dict):
                error = error.get('message')
            results[current]['output'].append(str(error or line))
    return results, shipped


def streamer_health(active, heartbeat, now, max_age):
    """(in_sync, alive) of a binlog streamer.

    in_sync: its last "caught up with the server" heartbeat is fresh, so the
    open file is complete up to now. alive: the unit runs and isn't stalled. A
    streamer that just started has no heartbeat yet: alive, not (yet) in sync.
    """
    if active != 'active':
        return False, False
    if heartbeat is None:
        return False, True
    fresh = (now - heartbeat) <= max_age
    return fresh, fresh

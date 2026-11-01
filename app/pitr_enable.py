"""
Enable-PITR wizard: pure decisions (no database, no SSH).

The wizard turns the prerequisite check (app.pitr.evaluate_*_prereqs) plus a
probe of how the database is installed into a *remediation plan*: one item
per failing check, each with an action backupx can take (or manual steps),
whether it needs a restart, and whether it needs a one-time admin login.

Config changes follow the owner's rules (docs/UX-OVERHAUL.md, P3):
- MySQL/MariaDB host installs: a drop-in file `zz-backupx-pitr.cnf` in the
  last-included conf.d directory (/etc/mysql/mariadb.conf.d, mysql.conf.d,
  conf.d, /etc/my.cnf.d); docker databases only when a conf directory is
  bind-mounted (else a compose snippet); managed databases (DigitalOcean...)
  get no config/restart options at all.
- MySQL 8: dynamic settings with SET PERSIST; MariaDB: SET GLOBAL + the
  drop-in (or folded into the drop-in when a restart happens anyway).
- PostgreSQL: ALTER SYSTEM (+ pg_reload_conf for archive_command /
  archive_timeout, a restart for wal_level / archive_mode); a conf.d drop-in
  only when ALTER SYSTEM is disabled (PG 17 allow_alter_system=off).

The plan dict is hashed (app.security.plan_hash); only things the user agrees
to go in it (no free-space numbers or timestamps that drift).
"""

import os
import re
from datetime import datetime, timedelta, timezone

from . import pitr

DROPIN_NAME = 'zz-backupx-pitr.cnf'
PG_CONFD_NAME = 'zz-backupx-pitr.conf'
CHARSET_DROPIN_NAME = 'zz-backupx-client-charset.cnf'
CONFIG_BACKUP_ROOT = '/var/lib/backupx-pitr/config-backups'

# Client programs that keep the character set when it moves out of [client]
CHARSET_SECTIONS = ('mysql', 'mysqldump', 'mysqladmin', 'mysqlcheck', 'mysqlimport', 'mysqlshow')

# Where a docker database may have its conf dir bind-mounted (inside the container)
DOCKER_CONF_DESTS = ('/etc/mysql/conf.d', '/etc/mysql/mariadb.conf.d', '/etc/mysql/mysql.conf.d')
# Host conf.d candidates, in the order we prefer when the include chain is unknown
HOST_DROPIN_DIRS = ('/etc/mysql/mariadb.conf.d', '/etc/mysql/mysql.conf.d', '/etc/mysql/conf.d', '/etc/my.cnf.d')

MANAGED_PATTERNS = (
    (r'\.db\.ondigitalocean\.com$', 'DigitalOcean'),
    (r'\.rds\.amazonaws\.com$', 'Amazon RDS'),
    (r'\.(mysql|postgres|mariadb)\.database\.azure\.com$', 'Azure'),
    (r'\.aivencloud\.com$', 'Aiven'),
    (r'\.psdb\.cloud$', 'PlanetScale'),
)

PHASE_APPLY = 'Apply'
PHASE_RESTART = 'Restart'
PHASE_STREAM = 'Stream'
PHASE_ANCHOR = 'Anchor'


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def settings():
    return {
        # Seconds the database may take to come back healthy after a restart
        'health_timeout': max(30, _env_int('PITR_MAINT_HEALTH_TIMEOUT', 300)),
        # Never roll back while the server is still starting / in crash recovery; give up waiting
        # (no rollback, critical alert) after this many seconds
        'recovery_max_wait': max(60, _env_int('PITR_MAINT_RECOVERY_MAX_WAIT', 7200)),
        'poll_interval': max(1, _env_int('PITR_MAINT_POLL_INTERVAL', 5)),
        # A successful backup newer than this counts as the "fresh backup" before a restart
        'fresh_backup_max_age': _env_int('PITR_FRESH_BACKUP_MAX_AGE', 3600),
        # The wizard's cached check results stay valid this long
        'plan_cache_seconds': _env_int('PITR_PLAN_CACHE_SECONDS', 900),
        'binlog_basename': (os.environ.get('PITR_BINLOG_BASENAME') or 'mysql-bin').strip(),
        'binlog_expire_days': max(1, _env_int('PITR_BINLOG_EXPIRE_DAYS', 2)),
        'managed_hosts': (os.environ.get('PITR_MANAGED_HOSTS') or '').strip(),
        # Minutes the wizard waits for state ok after the anchor
        'ok_wait_seconds': _env_int('PITR_ENABLE_OK_WAIT', 600),
    }


def managed_provider(host, extra_pattern=''):
    """Provider name if the database host is a managed service, else None."""
    host = (host or '').strip().lower()
    if not host:
        return None
    for pattern, name in MANAGED_PATTERNS:
        if re.search(pattern, host):
            return name
    if extra_pattern:
        try:
            if re.search(extra_pattern, host):
                return 'Managed'
        except re.error:
            pass
    return None


def is_local_host(host, server_host, local_ips):
    host = (host or '').strip().lower()
    if host in ('', 'localhost', '127.0.0.1', '::1') or host.startswith('127.'):
        return True
    if host == (server_host or '').strip().lower():
        return True
    return host in {ip.strip().lower() for ip in (local_ips or '').split() if ip.strip()}


def parse_mounts(text):
    """'dest|source|rw|type;...' (docker inspect template) -> [{dest, source, rw, type}]."""
    mounts = []
    for part in (text or '').split(';'):
        bits = part.strip().split('|')
        if len(bits) >= 3 and bits[0]:
            mounts.append({'dest': bits[0].rstrip('/'), 'source': bits[1], 'rw': bits[2].lower() == 'true',
                           'type': bits[3] if len(bits) > 3 else ''})
    return mounts


def docker_conf_mount(mounts):
    """The bind/volume mount of a MySQL conf.d directory, writable, or None."""
    for dest in DOCKER_CONF_DESTS:
        for m in mounts:
            if m['dest'] == dest and m['rw'] and m['source']:
                return m
    return None


def host_dropin_dir(includedirs, existing_dirs):
    """The conf.d directory read last (our zz- file there wins), among the existing ones."""
    existing = [d.rstrip('/') for d in (existing_dirs or '').split() if d.strip()]
    chain = [d.rstrip('/') for d in (includedirs or '').split() if d.strip()]
    for d in reversed(chain):
        if d in existing:
            return d
    for d in HOST_DROPIN_DIRS:
        if d in existing:
            return d
    return None


def server_version_tuple(text):
    return pitr._version_tuple(text)


# ---------------------------------------------------------------------------
# Target: how the database runs and what backupx may do with it
# ---------------------------------------------------------------------------

def mysql_target(db_config, server, prereq, host, cfg=None):
    """host: facts from maint_scripts.mysql_host_probe_script."""
    cfg = cfg or settings()
    version = prereq.get('server_version') or host.get('mysqld_version') or ''
    flavor = pitr.server_flavor(version or host.get('mysqld_version', ''))
    container = (db_config.get('docker_container') or '').strip()
    db_host = db_config.get('host') or ''
    root_ok = host.get('uid') == '0' or host.get('sudo_uid') == '0'
    out = {'database': db_config.get('name'), 'server': server.get('name') or server.get('host'),
           'flavor': flavor, 'version': version, 'service': None, 'container': None,
           'config_method': 'manual', 'config_path': None, 'managed_provider': None,
           'restart_supported': False, 'restart_unsupported_reason': None, 'install': 'host'}
    provider = None if container else managed_provider(db_host, cfg['managed_hosts'])
    if provider:
        out.update(install='managed', managed_provider=provider,
                   restart_unsupported_reason=f'Managed database ({provider}): the provider owns the server '
                                              'configuration and restarts; change settings in its console.')
        return out
    if container:
        out.update(install='docker', container=container)
        if host.get('container_found') != '1':
            out['restart_unsupported_reason'] = f'Container {container} not found on {out["server"]}.'
            return out
        mount = docker_conf_mount(parse_mounts(host.get('docker_mounts')))
        if mount:
            out.update(config_method='bind_mount', config_path=mount['source'].rstrip('/') + '/' + DROPIN_NAME,
                       config_dest=mount['dest'] + '/' + DROPIN_NAME)
            if host.get('docker') == '1':
                out['restart_supported'] = True
            else:
                out['restart_unsupported_reason'] = 'docker is not usable by the SSH user (or sudo -n docker).'
        else:
            out['restart_unsupported_reason'] = (
                'No conf.d directory of the container is bind-mounted, so backupx cannot change its '
                'configuration (it never recreates containers). Use the compose snippet.')
        return out
    if not is_local_host(db_host, server.get('host'), host.get('local_ips')):
        out.update(install='remote', restart_unsupported_reason=(
            f'The database ({db_host}) does not run on {out["server"]}; change its configuration on its own '
            'host.'))
        return out
    out['service'] = host.get('unit') or None
    dropin_dir = host_dropin_dir(host.get('includedirs'), host.get('dropin_dirs'))
    if dropin_dir:
        out.update(config_method='dropin', config_path=dropin_dir + '/' + DROPIN_NAME)
    reasons = []
    if not root_ok:
        reasons.append('the SSH user is not root and has no passwordless sudo')
    if host.get('systemd') != '1':
        reasons.append('the server does not run systemd')
    if not out['service']:
        reasons.append('no mariadb/mysql systemd unit found')
    if not dropin_dir:
        reasons.append('no conf.d directory is included by the MySQL configuration')
    if reasons:
        out['restart_unsupported_reason'] = 'backupx cannot restart it: ' + '; '.join(reasons) + '.'
    else:
        out['restart_supported'] = True
    return out


def pg_target(db_config, server, prereq_facts, host, cfg=None):
    root_ok = host.get('uid') == '0' or host.get('sudo_uid') == '0'
    out = {'database': db_config.get('name'), 'server': server.get('name') or server.get('host'),
           'flavor': 'postgres', 'version': prereq_facts.get('server_version') or '', 'install': 'host',
           'service': host.get('unit') or None, 'container': None, 'config_method': 'manual',
           'config_path': None, 'managed_provider': None, 'restart_supported': False,
           'restart_unsupported_reason': None}
    if (host.get('allow_alter_system') or 'on') != 'off':
        out.update(config_method='alter_system', config_path=(host.get('data_directory') or '$PGDATA')
                   + '/postgresql.auto.conf')
    elif host.get('include_dir'):
        out.update(config_method='conf_d', config_path=host['include_dir'].rstrip('/') + '/' + PG_CONFD_NAME)
    reasons = []
    if not root_ok:
        reasons.append('the SSH user is not root and has no passwordless sudo')
    if host.get('systemd') != '1':
        reasons.append('the server does not run systemd')
    if not out['service']:
        reasons.append('no postgresql systemd unit found')
    if out['config_method'] == 'manual':
        reasons.append('ALTER SYSTEM is disabled and postgresql.conf has no include_dir')
    if reasons:
        out['restart_unsupported_reason'] = 'backupx cannot restart it: ' + '; '.join(reasons) + '.'
    else:
        out['restart_supported'] = True
    return out


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _item(id_, check, status, title, action, downtime='none', auto=True, needs_admin=False, description='',
          settings_=None, sql=None, manual=None, default_selected=True, **extra):
    out = {'id': id_, 'check': check, 'status': status, 'title': title, 'action': action, 'downtime': downtime,
           'auto': bool(auto), 'needs_admin': bool(needs_admin), 'description': description,
           'settings': settings_ or None, 'sql': sql or None, 'manual': manual,
           'default_selected': bool(auto and default_selected)}
    out.update(extra)
    return out


def _checks_by_name(checks):
    return {c['check']: c for c in checks or []}


def _has_privilege(grants, *privileges):
    return any(pitr._grants_have(grants, p) for p in privileges)


def mysql_expire_setting(flavor, version, days):
    """(name, value) of the binlog expiry option for this server."""
    v = server_version_tuple(version) or (0, 0, 0)
    if flavor == 'mariadb' and v[:2] < (10, 6):
        return 'expire_logs_days', str(days)
    return 'binlog_expire_logs_seconds', str(days * 86400)


def compose_snippet(settings_, container):
    args = ' '.join(f'--{k.replace("_", "-")}={v}' for k, v in settings_.items())
    lines = [f'# docker-compose.yml, service of the database container {container}:',
             '    command: ' + args,
             '# or mount a conf.d directory and put this in a .cnf file there:',
             '[mysqld]'] + [f'{k} = {v}' for k, v in settings_.items()]
    return lines


def _manual_config(target, settings_, restart):
    """Manual steps for config lines backupx can't write itself."""
    lines = ['[mysqld]'] + [f'{k} = {v}' for k, v in settings_.items()]
    install = target['install']
    if install == 'managed':
        return {'steps': [f"{target['managed_provider']} manages this server's configuration: set these in its "
                          "control panel (or ask the provider)."], 'commands': lines}
    if install == 'docker':
        return {'steps': ['Add these options to the database container (it was not created with a bind-mounted '
                          'conf.d directory, and backupx never recreates containers).',
                          'Recreate the container in a quiet moment (docker compose up -d).'] if restart else
                ['Add these options to the container configuration.'],
                'commands': compose_snippet(settings_, target.get('container'))}
    steps = ['Add these lines to the [mysqld] section of the database configuration on its host.']
    if restart:
        steps.append('Restart the database in a quiet moment.')
    return {'steps': steps, 'commands': lines}


def mysql_items(prereq, target, host, job, cfg=None, restart_selected=None):
    """Remediation items for a MySQL/MariaDB job.

    prereq: pitr_service.check_prereqs result (checks + facts); host: probe facts.
    restart_selected: whether the selection contains a restart item (decides how the
    MariaDB dynamic settings are applied); None = assume the default selection.
    """
    cfg = cfg or settings()
    checks = _checks_by_name(prereq.get('checks'))
    facts = prereq.get('facts') or {}
    flavor = target['flavor']
    version = target['version']
    v = server_version_tuple(version) or (0, 0, 0)
    is_mysql8 = flavor == 'mysql' and v[:2] >= (8, 0)
    can_config = target['config_method'] in ('dropin', 'bind_mount')
    can_restart = can_config and target['restart_supported']
    items = []

    def manual_from(check, steps=None):
        return {'steps': steps or [check.get('fix') or 'See the prerequisite check.'], 'commands': []}

    conn = checks.get('Database connection')
    if conn and conn['status'] == 'fail':
        items.append(_item('mysql.connection', conn['check'], 'fail', 'Make the database reachable', 'manual',
                           auto=False, manual=manual_from(conn)))
        return items

    restart_items = []
    lb = checks.get('Binary log (log_bin)')
    if lb and lb['status'] == 'fail':
        settings_ = {'log_bin': cfg['binlog_basename']}
        expire = checks.get('Binlog retention on the server')
        current_expire = None
        try:
            current_expire = int(float(facts.get('binlog_expire_seconds') or 0))
        except ValueError:
            pass
        if expire is None or current_expire in (None, 0) or (expire and expire['status'] != 'ok'):
            name, value = mysql_expire_setting(flavor, version, cfg['binlog_expire_days'])
            settings_[name] = value
        restart_items.append(_item(
            'mysql.log_bin', lb['check'], 'fail', 'Turn on the binary log', 'dropin', downtime='restart',
            auto=can_restart, settings_=settings_,
            description=(f"Writes {', '.join(f'{k}={v}' for k, v in settings_.items())} to {target['config_path']} "
                         f"and restarts {target.get('service') or target.get('container')}." if can_restart else
                         'The binary log can only be switched on in the configuration, with a restart.'),
            manual=_manual_config(target, settings_, True)))
    sid = checks.get('server_id')
    if sid and sid['status'] == 'fail':
        restart_items.append(_item(
            'mysql.server_id', sid['check'], 'fail', 'Set a server_id', 'dropin', downtime='restart',
            auto=can_restart, settings_={'server_id': '1'},
            description=f"Writes server_id=1 to {target['config_path']} (restart)." if can_restart else
            'server_id must be set in the configuration (restart).',
            manual=_manual_config(target, {'server_id': '1'}, True)))
    items.extend(restart_items)
    if restart_selected is None:
        restart_selected = any(i['auto'] for i in restart_items)

    grants = facts.get('grants', '')
    can_set_global = _has_privilege(grants, 'SUPER', 'SYSTEM_VARIABLES_ADMIN')

    def dynamic(id_, check, title, name, value):
        """binlog_format / binlog_row_image / expiry: SET PERSIST (MySQL 8) or SET GLOBAL + drop-in (MariaDB)."""
        if target['install'] == 'managed':
            return _item(id_, check['check'], check['status'], title, 'manual', auto=False,
                         manual=_manual_config(target, {name: value}, False))
        if is_mysql8:
            return _item(id_, check['check'], check['status'], title, 'sql', needs_admin=not can_set_global,
                         sql=[f"SET PERSIST {name} = {pitr._sql_string(value)}"],
                         description=f'SET PERSIST {name} (no restart; survives restarts in mysqld-auto.cnf).',
                         manual={'steps': ['Run as an admin user:'],
                                 'commands': [f"SET PERSIST {name} = {pitr._sql_string(value)};"]})
        if not can_config:
            # Docker without a conf mount / remote host: SET GLOBAL now, config by hand
            return _item(id_, check['check'], check['status'], title, 'sql', needs_admin=not can_set_global,
                         sql=[f"SET GLOBAL {name} = {pitr._sql_string(value)}"],
                         description=f'SET GLOBAL {name} (no restart). It is lost at the next restart unless '
                                     'you also add it to the configuration (see the manual steps).',
                         manual=_manual_config(target, {name: value}, False))
        if restart_selected and can_restart:
            return _item(id_, check['check'], check['status'], title, 'dropin', downtime='restart',
                         settings_={name: value},
                         description=f'Writes {name}={value} to {target["config_path"]} (applied by the restart).',
                         manual=_manual_config(target, {name: value}, False))
        return _item(id_, check['check'], check['status'], title, 'dropin_sql', needs_admin=not can_set_global,
                     settings_={name: value}, sql=[f"SET GLOBAL {name} = {pitr._sql_string(value)}"],
                     description=f'SET GLOBAL {name} now and write it to {target["config_path"]} '
                                 '(no restart; validated).',
                     manual=_manual_config(target, {name: value}, False))

    fmt = checks.get('binlog_format')
    if fmt and fmt['status'] in ('fail', 'warn'):
        items.append(dynamic('mysql.binlog_format', fmt, 'Use row-based binary logs', 'binlog_format', 'ROW'))
    ri = checks.get('binlog_row_image')
    if ri and ri['status'] in ('fail', 'warn'):
        items.append(dynamic('mysql.binlog_row_image', ri, 'Log full row images', 'binlog_row_image', 'FULL'))
    exp = checks.get('Binlog retention on the server')
    if exp and exp['status'] == 'warn' and not (lb and lb['status'] == 'fail'):
        name, value = mysql_expire_setting(flavor, version, max(cfg['binlog_expire_days'], 1))
        items.append(dynamic('mysql.binlog_expire', exp, 'Keep binary logs longer on the server', name, value))

    missing = [p for p in ('REPLICATION SLAVE', 'REPLICATION CLIENT', 'RELOAD')
               if (checks.get(f'Privilege {p}') or {}).get('status') in ('fail', 'warn')]
    if missing:
        account = pitr.mysql_account(facts.get('user', ''))
        sql = f"GRANT {', '.join(missing)} ON *.* TO {account}"
        status = 'fail' if any(checks[f'Privilege {p}']['status'] == 'fail' for p in missing) else 'warn'
        items.append(_item('mysql.grants', 'Privileges', status, f"Grant {', '.join(missing)}", 'grant',
                           needs_admin=True, sql=[sql], account=account,
                           description=f'Runs {sql} with a one-time admin login (not stored).',
                           manual={'steps': ['Run as an admin user (e.g. root):'], 'commands': [sql + ';']}))

    # Client programs (binlog client / dump client / option files)
    charset_error = ''
    for name in ('mysqlbinlog starts', 'mysqlbinlog reads the host option files'):
        c = checks.get(name)
        if c and c['status'] in ('fail', 'warn') and 'default-character-set' in (c.get('current') or '') + (c.get('fix') or ''):
            charset_error = c
            break
    suggestion = prereq.get('suggested_client_image') or pitr.suggested_client_image(version)
    root_ok = host.get('uid') == '0' or host.get('sudo_uid') == '0'
    if charset_error:
        files = [f for f in (host.get('charset_files') or '').split() if f]
        auto = bool(files) and root_ok and target['install'] == 'host'
        value = host.get('charset_value') or 'utf8mb4'
        items.append(_item(
            'mysql.client_charset', charset_error['check'], charset_error['status'],
            'Move default-character-set out of [client]', 'client_charset', auto=auto,
            default_selected=charset_error['status'] == 'fail',
            files=files, charset=value,
            description=(f"Comments out default-character-set in the [client] section of {', '.join(files)} "
                         f"and sets it for {', '.join(CHARSET_SECTIONS)} in {CHARSET_DROPIN_NAME} instead "
                         "(backed up first; no restart).") if auto else
            'mysqlbinlog rejects default-character-set from the [client] section of the host option files.',
            manual={'steps': [charset_error.get('fix') or 'Move default-character-set from [client] to [mysql] '
                              'and [mysqldump].'], 'commands': []}))
    image_checks = [checks.get(n) for n in ('mysqlbinlog', 'mysqlbinlog matches the server',
                                             'mysqldump can record the binlog position')]
    image_checks = [c for c in image_checks if c and c['status'] in ('fail', 'warn')]
    if (charset_error and charset_error['check'] == 'mysqlbinlog starts') and not image_checks:
        image_checks = [charset_error]
    current_image = (job.get('pitr_client_image') or '').strip()
    if image_checks and not (db_config_container(target)) and suggestion and suggestion != current_image:
        docker_ok = (prereq.get('facts') or {}).get('docker') == '1' or prereq.get('docker')
        worst = 'fail' if any(c['status'] == 'fail' for c in image_checks) else 'warn'
        items.append(_item(
            'mysql.client_image', image_checks[0]['check'], worst, f'Use {suggestion} as the binlog client',
            'job_field', auto=bool(docker_ok), settings_={'pitr_client_image': suggestion},
            default_selected=not (charset_error and len(image_checks) == 1 and image_checks[0] is charset_error),
            description=f"Sets the job's client image to {suggestion}: mysqlbinlog/mysqldump then run from that "
                        'image (docker on the server), matching the database.',
            manual={'steps': ['Install docker on the server, or install the client matching the database: ' +
                              (image_checks[0].get('fix') or '')], 'commands': []}))

    for name, title in (('Root or passwordless sudo', 'Give backupx root or passwordless sudo'),
                        ('systemd', 'The streamer needs systemd'),
                        ('Docker', 'Docker is needed for the client image')):
        c = checks.get(name)
        if c and c['status'] == 'fail':
            items.append(_item('mysql.' + re.sub(r'\W+', '_', name.lower()).strip('_'), name, 'fail', title,
                               'manual', auto=False, manual=manual_from(c)))
    free = checks.get('Free space for the spool')
    if free and free['status'] == 'warn':
        items.append(_item('mysql.free_space', free['check'], 'warn', 'Free disk space under /var/lib', 'manual',
                           auto=False, manual=manual_from(free)))
    return items


def db_config_container(target):
    return target.get('install') == 'docker'


def pg_items(prereq, target, host, cfg=None):
    checks = _checks_by_name(prereq.get('checks'))
    items = []
    conn = checks.get('Database connection')
    if conn and conn['status'] == 'fail':
        return [_item('pg.connection', conn['check'], 'fail', 'Make PostgreSQL reachable', 'manual', auto=False,
                      manual={'steps': [conn.get('fix') or ''], 'commands': []})]
    ver = checks.get('PostgreSQL')
    if ver and ver['status'] == 'fail':
        items.append(_item('pg.version', ver['check'], 'fail', 'PostgreSQL 10 or newer', 'manual', auto=False,
                           manual={'steps': [ver.get('fix')], 'commands': []}))
    for name in ('Root or passwordless sudo', 'WAL-G build'):
        c = checks.get(name)
        if c and c['status'] == 'fail':
            items.append(_item('pg.' + re.sub(r'\W+', '_', name.lower()).strip('_'), name, 'fail',
                               'Give backupx root or passwordless sudo' if name.startswith('Root') else
                               'No WAL-G build for this host', 'manual', auto=False,
                               manual={'steps': [c.get('fix')], 'commands': []}))
    walg = checks.get('wal-g installed')
    root_ok = host.get('uid') == '0' or host.get('sudo_uid') == '0'
    if walg and walg['status'] != 'ok':
        items.append(_item('pg.walg_install', walg['check'], 'warn', 'Install wal-g', 'walg_install',
                           auto=root_ok, description="Installs the wal-g binary, its 0600 env file (S3 keys of the "
                                                     "job's first storage) and the archive script.",
                           manual={'steps': ['Needs root or passwordless sudo on the server.'], 'commands': []}))
    method = target['config_method']
    can_set = method in ('alter_system', 'conf_d')

    def conf_item(id_, check, title, name, value, restart):
        line = f"{name} = '{value}'"
        if method == 'alter_system':
            sql = [f"ALTER SYSTEM SET {name} = {pitr._sql_string(value)}"]
            how = f"ALTER SYSTEM SET {name} = '{value}'"
        else:
            sql = None
            how = f"writes {line} to {target['config_path']}"
        if restart:
            auto = can_set and target['restart_supported']
            desc = f"{how}, then restart {target.get('service') or 'PostgreSQL'}."
            action = 'alter_system_restart'
        else:
            auto = can_set and root_ok
            desc = f'{how}, then SELECT pg_reload_conf() (no restart).'
            action = 'alter_system_reload'
        return _item(id_, check['check'], check['status'], title, action, downtime='restart' if restart else 'none',
                     auto=auto, settings_={name: value}, sql=sql, description=desc,
                     manual={'steps': ['Add to postgresql.conf (or run as a superuser):' +
                                       (' then restart PostgreSQL.' if restart else ' then SELECT pg_reload_conf().')],
                             'commands': [line, f"ALTER SYSTEM SET {name} = '{value}';"]})

    for name, value, title, restart in (('wal_level', 'replica', 'Set wal_level = replica', True),
                                        ('archive_mode', 'on', 'Turn on WAL archiving', True),
                                        ('archive_command', pitr.WALG_ARCHIVE_COMMAND,
                                         'Archive WAL with wal-g', False),
                                        ('archive_timeout', '900', 'Archive at least every 15 minutes', False)):
        c = checks.get(name)
        if c and c['status'] in ('fail', 'warn'):
            items.append(conf_item('pg.' + name, c, title, name, value, restart))
    failures = checks.get('Archiver failures')
    if failures and failures['status'] == 'warn':
        items.append(_item('pg.archiver_failures', failures['check'], 'warn', 'Check past archiver failures',
                           'manual', auto=False, manual={'steps': [failures.get('fix')], 'commands': []}))
    return items


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class PlanError(ValueError):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.code = code


def parse_restart_at(value, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        at = pitr._utc(value)
    except (TypeError, ValueError):
        raise PlanError('restart_at must be an ISO date and time', 'invalid_restart_at')
    if at is None:
        raise PlanError('Pick a time for the restart window', 'invalid_restart_at')
    if at < now - timedelta(minutes=1):
        raise PlanError('The restart window is in the past', 'invalid_restart_at')
    if at > now + timedelta(days=60):
        raise PlanError('The restart window must be within 60 days', 'invalid_restart_at')
    return at.replace(second=0, microsecond=0)


def downtime_text(kind, target, needs_restart):
    if not needs_restart:
        return 'None'
    what = target.get('service') or target.get('container') or 'the database'
    if kind == 'postgres':
        return f'About 5-30 s: one restart of {what} (longer if it has to replay WAL after a crash)'
    return (f'About 10-60 s: one restart of {what} (longer if InnoDB has to recover or the buffer pool is '
            'large); connections are dropped')


def restart_section(kind, target, selected_items, mode, at):
    """The part of the plan the restart phase executes; hashed separately so a scheduled
    window can re-derive and compare it after the no-downtime items changed the state."""
    restart_items = [i for i in selected_items if i['downtime'] == 'restart']
    if not restart_items:
        return None
    merged = {}
    for item in restart_items:
        merged.update(item.get('settings') or {})
    return {
        'kind': kind,
        'items': sorted(i['id'] for i in restart_items),
        'settings': dict(sorted(merged.items())),
        'method': 'docker restart' if target['install'] == 'docker' else 'systemctl restart',
        'unit': target.get('service'), 'container': target.get('container'),
        'config_method': target['config_method'], 'config_path': target.get('config_path'),
        'mode': mode, 'at': at.isoformat() if at else None,
        'downtime': downtime_text(kind, target, True),
    }


def build_plan(job, kind, target, items, selected_ids, mode='now', at=None, fresh_backup=False,
               admin_mode=None):
    """The hashed plan for this selection. Raises PlanError for an invalid selection."""
    by_id = {i['id']: i for i in items}
    unknown = [i for i in selected_ids if i not in by_id]
    if unknown:
        raise PlanError(f"Unknown or no longer applicable item(s): {', '.join(unknown)}", 'invalid_items')
    manual = [i for i in selected_ids if not by_id[i]['auto']]
    if manual:
        raise PlanError(f"Item(s) can only be done by hand: {', '.join(manual)}", 'invalid_items')
    selected = [by_id[i] for i in [x['id'] for x in items] if i in set(selected_ids)]
    if mode not in ('now', 'at'):
        raise PlanError('restart must be "now" or "at"', 'invalid_restart_at')
    restart = restart_section(kind, target, selected, mode, at)
    if restart and not target['restart_supported']:
        raise PlanError(target.get('restart_unsupported_reason') or 'backupx cannot restart this database',
                        'invalid_items')
    if restart and mode == 'at' and not at:
        raise PlanError('Pick a time for the restart window', 'invalid_restart_at')
    steps = []
    no_downtime = [i for i in selected if i['downtime'] == 'none']
    for item in no_downtime:
        steps.append(f"{PHASE_APPLY}: {item['title']}" +
                     (' (one-time admin login, not stored)' if item['needs_admin'] else ''))
    if no_downtime:
        steps.append(f'{PHASE_APPLY}: re-run the prerequisite check')
    if restart:
        when = 'now' if mode == 'now' else f"in the window at {restart['at']}"
        steps += [
            f"{PHASE_RESTART} ({when}): back up the job first unless a successful backup is less than "
            f"{settings()['fresh_backup_max_age'] // 60} min old" + (' (forced: always)' if fresh_backup else ''),
            f"{PHASE_RESTART}: back up the database configuration on the server",
            f"{PHASE_RESTART}: " + ('ALTER SYSTEM SET ' if target['config_method'] == 'alter_system' else 'write ')
            + ', '.join(f'{k}={v}' for k, v in restart['settings'].items())
            + (f" to {restart['config_path']}" if target['config_method'] != 'alter_system' else ''),
            f"{PHASE_RESTART}: validate the configuration (stop here and undo the change if it is invalid)",
            f"{PHASE_RESTART}: {restart['method']} {restart['unit'] or restart['container']}",
            f"{PHASE_RESTART}: health check (connect + verify the settings); on failure restore the previous "
            "configuration, restart again and alert (never while the server is still recovering)",
        ]
    steps += [
        f'{PHASE_STREAM}: switch PITR on for the job and ' +
        ('install the binlog streamer; wait until it records the server' if kind == 'mysql' else
         'install wal-g and check archiving'),
        f'{PHASE_ANCHOR}: ' + ('anchored full dump (--source-data) to every storage' if kind == 'mysql' else
                               'WAL-G base backup (backup-push)'),
        f'{PHASE_ANCHOR}: wait until the PITR state is ok',
    ]
    plan = {
        'job_id': job['id'], 'job_name': job.get('name'), 'kind': kind,
        'target': {k: target.get(k) for k in ('database', 'server', 'install', 'flavor', 'version', 'service',
                                              'container', 'config_method', 'config_path', 'managed_provider')},
        'items': [{'id': i['id'], 'action': i['action'], 'downtime': i['downtime'], 'settings': i.get('settings'),
                   'sql': i.get('sql'), 'needs_admin': i['needs_admin'], 'files': i.get('files')}
                  for i in selected],
        'admin': bool(any(i['needs_admin'] for i in selected)),
        'restart': restart,
        'fresh_backup': 'always' if fresh_backup else 'if_older_than_1h',
        'steps': steps,
        'downtime': downtime_text(kind, target, bool(restart)),
    }
    return plan


def default_selection(items):
    return [i['id'] for i in items if i['default_selected']]


def fresh_backup_decision(last_success, now=None, max_age=3600, force=False):
    """'run' or 'skip' (a successful backup newer than max_age seconds)."""
    if force:
        return 'run'
    now = now or datetime.now(timezone.utc)
    try:
        last = pitr._utc(last_success) if last_success else None
    except (TypeError, ValueError):
        last = None
    if last and (now - last).total_seconds() < max_age:
        return 'skip'
    return 'run'


# ---------------------------------------------------------------------------
# Drop-in content, option-file analysis
# ---------------------------------------------------------------------------

def parse_cnf(text):
    """Our drop-in -> {section: {key: value}} (keys normalised to underscores)."""
    out, section = {}, None
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        m = re.match(r'^\[([^\]]+)\]$', line)
        if m:
            section = m.group(1).strip()
            out.setdefault(section, {})
            continue
        if section is None:
            continue
        key, _, value = line.partition('=')
        out[section][key.strip().replace('-', '_')] = value.strip()
    return out


def dropin_content(existing_text, settings_):
    """Merge settings into our drop-in's [mysqld] section (our file only)."""
    sections = parse_cnf(existing_text)
    mysqld = dict(sections.get('mysqld') or {})
    mysqld.update({k: str(v) for k, v in settings_.items()})
    lines = ['# Managed by backupx (point-in-time recovery). Restore the backup in',
             f'# {CONFIG_BACKUP_ROOT}/ or remove this file to undo.', '[mysqld]']
    lines += [f'{k} = {v}' for k, v in mysqld.items()]
    return '\n'.join(lines) + '\n'


def pg_confd_content(existing_text, settings_):
    values = {}
    for raw in (existing_text or '').splitlines():
        m = re.match(r"^\s*([a-z_]+)\s*=\s*(.*?)\s*$", raw)
        if m:
            values[m.group(1)] = m.group(2)
    for k, v in settings_.items():
        values[k] = "'" + str(v).replace("'", "''") + "'"
    return '# Managed by backupx (point-in-time recovery)\n' + ''.join(f'{k} = {v}\n' for k, v in values.items())


def charset_dropin(value):
    lines = ['# Managed by backupx: default-character-set moved here from [client]',
             '# (mysqlbinlog rejects it there).']
    for section in CHARSET_SECTIONS:
        lines += [f'[{section}]', f'default-character-set = {value}']
    return '\n'.join(lines) + '\n'


_OPT = re.compile(r'^--([A-Za-z0-9_-]+)(?:=(.*))?$')


def effective_options(print_defaults_lines):
    """`my_print_defaults --mysqld` output -> {option: value} with later lines winning.

    skip-X / disable-X turn X off ('OFF'); a bare --X is 'ON'.
    """
    eff = {}
    for line in print_defaults_lines or []:
        m = _OPT.match(line.strip())
        if not m:
            continue
        name = m.group(1).replace('-', '_')
        value = m.group(2)
        for prefix in ('skip_', 'disable_'):
            if name.startswith(prefix):
                eff[name[len(prefix):]] = 'OFF'
                break
        else:
            eff[name] = 'ON' if value is None else value
    return eff


def overridden_settings(eff, wanted):
    """Settings whose effective value (after every option file) isn't ours: [(name, effective)]."""
    bad = []
    for name, value in wanted.items():
        key = name.replace('-', '_')
        got = eff.get(key)
        if key == 'log_bin':
            if got in (None, 'OFF', '0'):
                bad.append((name, got or 'not set'))
            continue
        if got is None or str(got).strip("'\"").upper() != str(value).upper():
            bad.append((name, got if got is not None else 'not set'))
    return bad


# ---------------------------------------------------------------------------
# Health after a restart
# ---------------------------------------------------------------------------

MYSQL_RECOVERY = re.compile(r'crash recovery|Starting final batch to recover|Apply batch|Rolling back trx|'
                            r'InnoDB: To recover|log sequence number .* is in the future|Recovering after a crash|'
                            r'InnoDB: Starting recovery|Parsing redo log|Applying redo', re.I)
PG_RECOVERY = re.compile(r'database system was interrupted|redo starts at|automatic recovery in progress|'
                         r'database system is starting up|in recovery mode|not yet accepting connections|'
                         r'consistent recovery state', re.I)

HEALTHY = 'healthy'
WAIT = 'wait'
CRASHED = 'crashed'
UNHEALTHY = 'unhealthy'
RECOVERY_TIMEOUT = 'recovery_timeout'


def verify_settings(kind, values, wanted):
    """[(name, got)] of wanted settings not in effect. values: {name: value} as the server reports them."""
    bad = []
    for name, want in (wanted or {}).items():
        got = values.get(name)
        if kind == 'mysql':
            if name == 'log_bin':
                if not pitr._on(got):
                    bad.append((name, got))
                continue
            if name in ('expire_logs_days', 'binlog_expire_logs_seconds'):
                try:
                    if int(float(got or -1)) != int(float(want)):
                        bad.append((name, got))
                except ValueError:
                    bad.append((name, got))
                continue
        if got is None or str(got).strip().lower() != str(want).strip().lower():
            bad.append((name, got))
    return bad


def health_decision(kind, status, wanted, elapsed, cfg=None):
    """What to do after a restart, from one status poll.

    status: {state: active|activating|failed|inactive|deactivating|running|restarting|exited|...,
             sub: systemd sub state, connected: bool, values: {...}, log: [lines], restarts: int}
    Returns (decision, reason). Never CRASHED while the server is starting or recovering:
    those are WAIT until health_timeout, then RECOVERY_TIMEOUT only after recovery_max_wait
    (and RECOVERY_TIMEOUT means: do not roll back, alert).
    """
    cfg = cfg or settings()
    state = (status.get('state') or '').lower()
    sub = (status.get('sub') or '').lower()
    if status.get('fresh') is False and state in ('active', 'running', 'deactivating', ''):
        # Still the instance from before the restart (the restart job hasn't begun yet)
        if elapsed < cfg['health_timeout']:
            return WAIT, 'restart pending'
        return UNHEALTHY, f'the restart did not happen within {int(elapsed)} s'
    log = '\n'.join(status.get('log') or [])
    recovering = bool((MYSQL_RECOVERY if kind == 'mysql' else PG_RECOVERY).search(log))
    if status.get('connected'):
        bad = verify_settings(kind, status.get('values') or {}, wanted)
        if not bad:
            return HEALTHY, 'connected; settings in effect'
        return UNHEALTHY, 'running, but not in effect: ' + ', '.join(f'{n}={g}' for n, g in bad)
    crashed = state in ('failed', 'inactive', 'exited', 'dead') or sub == 'auto-restart' \
        or (state == 'restarting' and int(status.get('restarts') or 0) >= 1)
    if crashed and not recovering:
        return CRASHED, f"the server is not running ({state}{'/' + sub if sub else ''})"
    starting = state in ('activating', 'active', 'running', 'reloading', 'restarting', 'created') or recovering
    if starting:
        if elapsed < cfg['health_timeout']:
            return WAIT, 'starting' + (' (crash recovery in progress)' if recovering else '')
        if recovering or state in ('activating', 'reloading'):
            if elapsed < cfg['recovery_max_wait']:
                return WAIT, 'still starting after the health timeout' + \
                    (' (crash recovery in progress: not rolling back)' if recovering else ' (not rolling back)')
            return RECOVERY_TIMEOUT, 'still starting/recovering after the maximum wait; not rolled back'
        return UNHEALTHY, f'up ({state}) but not accepting connections after {int(elapsed)} s'
    if elapsed < cfg['health_timeout']:
        return WAIT, f'state {state or "unknown"}'
    return CRASHED, f'not running after {int(elapsed)} s ({state or "unknown"})'


def phase_of_step(step):
    """Wizard phase from a task step text ('Apply: ...', 'Restart: ...')."""
    text = (step or '').strip()
    for phase in (PHASE_APPLY, PHASE_RESTART, PHASE_STREAM, PHASE_ANCHOR):
        if text.startswith(phase):
            return phase.lower()
    return None

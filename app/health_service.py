"""
Health checks and fixes: database rows, probes (restic/SSH) and the fix tasks.

Pure decisions live in app.health. This module:

- gathers a state snapshot (cheap: database only) and runs the detectors,
  merging the stored `health_findings` rows (first_seen, snooze, alerts);
- runs the expensive probes (SSH login, restic cat config / locks / keys,
  weekly `restic check`, daily `restic stats`, PITR prerequisites) in the
  hourly sweep or on demand, caching results in `health_probes`;
- sends critical-only alerts (one per issue, plus one when it clears);
- defines the fix catalogue: each fix is a task kind `fix.<action>` with a
  tier and a plan() re-derived from current state on the server, both when
  the user previews it and again when the task runs.

app.main imports this module; app.main's helpers are imported lazily (_m()).
"""

import json
import logging
import os
import re
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import health
from . import tasks

logger = logging.getLogger(__name__)


def _m():
    from . import main
    return main


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def settings():
    return {
        'sweep_minutes': max(10, _env_int('HEALTH_SWEEP_MINUTES', 60)),
        'alert_seconds': max(60, _env_int('HEALTH_ALERT_INTERVAL', 300)),
        'check_days': max(1, _env_int('HEALTH_CHECK_DAYS', 7)),
        'stats_hours': max(1, _env_int('HEALTH_STATS_HOURS', 24)),
        'probe_timeout': max(10, _env_int('HEALTH_PROBE_TIMEOUT', 60)),
        'check_timeout': max(60, _env_int('HEALTH_CHECK_TIMEOUT', 3600)),
        'alert_types': [t.strip() for t in os.environ.get('HEALTH_ALERT_CHANNELS', 'telegram').split(',')
                        if t.strip()],
        'retry_backoff': [int(x) for x in re.findall(r'\d+', os.environ.get('FIX_RETRY_BACKOFF', '30,120'))][:5],
        'stale_lock_age': max(60, _env_int('STALE_LOCK_MIN_AGE', health.STALE_LOCK_MIN_AGE)),
    }


def _now():
    return datetime.now(timezone.utc)


def _naive(dt):
    """Aware -> naive UTC for TIMESTAMP columns."""
    if dt is None:
        return None
    dt = health.utc(dt)
    return dt.replace(tzinfo=None) if dt else None


def _iso(value):
    t = health.utc(value)
    return t.isoformat() if t else None


def _json(text, default):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _db():
    return _m().get_db()


# ---------------------------------------------------------------------------
# Small persisted values
# ---------------------------------------------------------------------------

HEARTBEAT_KEY = 'scheduler_heartbeat'
BACKUPX_HOSTS_KEY = 'backupx_hostnames'
SWEEP_KEY = 'health_last_sweep'


def _set_setting(key, value):
    db = _db()
    db.execute('INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP) '
               'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP',
               (key, value))
    db.commit()


def _get_setting(key, default=None):
    row = _db().fetchone('SELECT value FROM app_settings WHERE key = %s', (key,))
    return row['value'] if row else default


def record_scheduler_heartbeat():
    """Called by the scheduler leader on every lock tick. Never raises."""
    try:
        _set_setting(HEARTBEAT_KEY, _now().isoformat())
    except Exception as e:
        logger.debug(f"Scheduler heartbeat not recorded: {e}")
        try:
            _db().rollback()
        except Exception:
            pass


def remember_backupx_hostname():
    """Record this container's hostname: restic locks it leaves carry it (stale-lock rule)."""
    try:
        name = socket.gethostname().strip().lower()
        hosts = _json(_get_setting(BACKUPX_HOSTS_KEY), [])
        if name and name not in hosts:
            hosts = (hosts + [name])[-50:]
            _set_setting(BACKUPX_HOSTS_KEY, json.dumps(hosts))
    except Exception as e:
        logger.debug(f"Can't record the backupx hostname: {e}")


def backupx_hostnames():
    try:
        hosts = set(_json(_get_setting(BACKUPX_HOSTS_KEY), []))
    except Exception:
        hosts = set()
    hosts.add(socket.gethostname().strip().lower())
    return hosts


def record_channel_result(channel_id, error=None, secrets=()):
    """Remember a notification channel's last delivery result. Never raises."""
    if not channel_id:
        return
    try:
        db = _db()
        if error:
            text = tasks.redact_text(str(error), secrets)[:500]
            db.execute('UPDATE notification_channels SET last_error = %s, last_error_at = %s WHERE id = %s',
                       (text, tasks.utcnow(), channel_id))
        else:
            db.execute('UPDATE notification_channels SET last_success_at = %s WHERE id = %s',
                       (tasks.utcnow(), channel_id))
        db.commit()
    except Exception as e:
        logger.debug(f"Can't record notification result: {e}")
        try:
            _db().rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Probes cache
# ---------------------------------------------------------------------------

def save_probe(key, data, checked_at=None):
    db = _db()
    db.execute('INSERT INTO health_probes (key, data, checked_at) VALUES (%s, %s, %s) '
               'ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, checked_at = EXCLUDED.checked_at',
               (key, json.dumps(data, default=str), checked_at or tasks.utcnow()))
    db.commit()


def delete_probes(prefix):
    db = _db()
    db.execute('DELETE FROM health_probes WHERE key LIKE %s', (prefix.replace('%', r'\%') + '%',))
    db.commit()


def load_probes():
    rows = _db().fetchall('SELECT key, data, checked_at FROM health_probes')
    return {r['key']: {'data': _json(r['data'], {}), 'checked_at': r['checked_at']} for r in rows}


# ---------------------------------------------------------------------------
# State snapshot (cheap: database only)
# ---------------------------------------------------------------------------

def _job_storages(job, configs):
    """Active storages of a job without secrets: [{id, name, repo_url}] (+ the full targets, for fixes)."""
    m = _m()
    out = []
    prefix = job.get('backup_prefix') or job.get('id', '')
    for config_id in job.get('s3_config_ids') or []:
        cfg = configs.get(config_id)
        if not cfg or cfg.get('status') == 'inactive':
            continue
        out.append({'id': cfg['id'], 'name': cfg['name'],
                    'repo_url': m.restic_repo_url(cfg['endpoint'], cfg['bucket'], prefix)})
    if not job.get('s3_config_ids') and job.get('s3_endpoint'):
        out.append({'id': '', 'name': job['s3_endpoint'],
                    'repo_url': m.restic_repo_url(job['s3_endpoint'], job.get('s3_bucket'), prefix)})
    return out


def collect_state(now=None):
    m = _m()
    from . import pitr
    now = now or _now()
    jobs = m._annotate_overdue(m.load_jobs())
    jobs = {jid: dict(j, id=jid) for jid, j in jobs.items()}
    configs = {c['id']: c for c in m.load_s3_configs()}
    db = _db()
    latest = {}
    for r in db.fetchall('SELECT DISTINCT ON (job_id, storage_id) * FROM run_results '
                         'ORDER BY job_id, storage_id, finished_at DESC NULLS LAST, id DESC'):
        latest.setdefault(r['job_id'], {})[r['storage_id'] or ''] = dict(r)
    try:
        retention_runs = m.latest_retention_runs()
    except Exception as e:
        logger.debug(f"retention runs unavailable: {e}")
        retention_runs = {}
    pitr_rows = {}
    for r in db.fetchall('SELECT * FROM pitr_state'):
        row = dict(r)
        row['info'] = _json(row.get('info'), {})
        pitr_rows[row['job_id']] = row
    try:
        channels = m.load_notification_channels()
    except Exception:
        channels = []
    servers = {s['id']: s for s in m.load_servers()}
    in_use = {j.get('server_id') for j in jobs.values()
              if j.get('server_id') and (j.get('schedule_enabled') or j.get('pitr_enabled'))}
    interval = getattr(m, 'SCHEDULER_LOCK_INTERVAL', 15)
    return {
        'now': now,
        'jobs': jobs,
        'job_storages': {jid: _job_storages(j, configs) for jid, j in jobs.items()},
        'latest_results': latest,
        'retention': retention_runs,
        'pitr': pitr_rows,
        'pitr_heartbeat_max_age': pitr.settings()['heartbeat_max_age'],
        'pg_conf_changes': pitr.postgres_conf_changes(),
        'scheduler': {'heartbeat_at': _get_setting(HEARTBEAT_KEY), 'max_age': max(120, 4 * interval + 30)},
        'channels': channels,
        'servers': servers,
        'servers_in_use': in_use,
        'probes': load_probes(),
    }


# ---------------------------------------------------------------------------
# Findings (persisted issue rows)
# ---------------------------------------------------------------------------

def load_findings():
    return {r['issue_id']: dict(r) for r in _db().fetchall('SELECT * FROM health_findings')}


def sync_findings(issues, findings=None, now=None):
    """Upsert open issues, resolve the ones that went away. Returns the findings map (updated)."""
    now = now or tasks.utcnow()
    findings = findings if findings is not None else load_findings()
    db = _db()
    current = set()
    for issue in issues:
        iid = issue.id
        current.add(iid)
        row = findings.get(iid)
        evidence = json.dumps({'evidence': issue.evidence, 'title': issue.title}, default=str)
        if row is None or row.get('resolved_at') is not None:
            db.execute(
                '''INSERT INTO health_findings (issue_id, family, kind, resource_type, resource_id, title,
                                                severity, alert, first_seen, last_seen, evidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (issue_id) DO UPDATE SET kind = EXCLUDED.kind, title = EXCLUDED.title,
                       severity = EXCLUDED.severity, alert = EXCLUDED.alert, first_seen = EXCLUDED.first_seen,
                       last_seen = EXCLUDED.last_seen, evidence = EXCLUDED.evidence, resolved_at = NULL,
                       alerted_at = NULL, resolved_alerted_at = NULL''',
                (iid, issue.family, issue.kind, issue.resource.get('type'), issue.resource.get('id'), issue.title,
                 issue.severity, issue.alert, now, now, evidence))
            findings[iid] = dict(row or {}, issue_id=iid, family=issue.family, kind=issue.kind,
                                 resource_type=issue.resource.get('type'), resource_id=issue.resource.get('id'),
                                 title=issue.title, severity=issue.severity, alert=issue.alert, first_seen=now,
                                 last_seen=now, resolved_at=None, alerted_at=None, resolved_alerted_at=None,
                                 snoozed_until=(row or {}).get('snoozed_until'))
        else:
            stale = row.get('last_seen') is None or (now - row['last_seen']).total_seconds() >= 60
            if stale or row.get('severity') != issue.severity or row.get('kind') != issue.kind \
                    or row.get('title') != issue.title:
                db.execute('UPDATE health_findings SET last_seen = %s, severity = %s, kind = %s, title = %s, '
                           'alert = %s, evidence = %s WHERE issue_id = %s',
                           (now, issue.severity, issue.kind, issue.title, issue.alert, evidence, iid))
                row.update(last_seen=now, severity=issue.severity, kind=issue.kind, title=issue.title,
                           alert=issue.alert)
    for iid, row in findings.items():
        if iid not in current and row.get('resolved_at') is None:
            db.execute('UPDATE health_findings SET resolved_at = %s WHERE issue_id = %s AND resolved_at IS NULL',
                       (now, iid))
            row['resolved_at'] = now
    # Forget long-resolved rows (their alerts are done)
    db.execute("DELETE FROM health_findings WHERE resolved_at < %s AND (alerted_at IS NULL OR "
               "resolved_alerted_at IS NOT NULL)", (now - timedelta(days=30),))
    db.commit()
    return findings


def _active_fix_tasks():
    rows = _db().fetchall("SELECT * FROM tasks WHERE kind LIKE 'fix.%%' AND status IN ('queued', 'running')")
    out = {}
    for r in rows:
        params = _json(r.get('params'), {})
        if params.get('issue_id'):
            out[params['issue_id']] = tasks.serialize(r)
    return out


def fix_info(action):
    fix = FIXES.get(action) if action else None
    if not fix:
        return None
    return {'action': fix.action, 'tier': fix.tier, 'label': fix.label, 'description': fix.description}


def serialize_issue(issue, finding=None, active=None):
    d = issue.to_dict()
    finding = finding or {}
    fix = fix_info(issue.fix)
    if fix is None and issue.manual:
        fix = {'action': 'manual', 'tier': 'manual', 'label': 'How to fix', 'description': ''}
    out = {
        'id': d['id'], 'kind': d['kind'], 'family': d['family'], 'title': d['title'],
        'explanation': d['explanation'], 'severity': d['severity'], 'resource': d['resource'],
        'first_seen': _iso(finding.get('first_seen')) or _now().isoformat(),
        'last_seen': _iso(finding.get('last_seen')) or _now().isoformat(),
        'snoozed_until': None, 'evidence': d['evidence'], 'evidence_text': d['evidence_text'],
        'fix': fix, 'manual': d['manual'], 'wizard': d.get('wizard'), 'active_task': (active or {}).get(d['id']),
    }
    snoozed = health.utc(finding.get('snoozed_until'))
    if snoozed and snoozed > _now():
        out['snoozed_until'] = snoozed.isoformat()
    return out


def current_issues(state=None):
    """[Issue] for the current state (no writes)."""
    return health.detect(state or collect_state())


def find_issue(issue_id, state=None):
    for issue in current_issues(state):
        if issue.id == issue_id:
            return issue
    return None


def evaluate(sync=True, severity=None, resource_type=None, resource_id=None, include_snoozed=False):
    """What GET /api/health returns: {summary, issues}."""
    state = collect_state()
    issues = health.detect(state)
    findings = sync_findings(issues) if sync else load_findings()
    active = _active_fix_tasks()
    out, snoozed = [], 0
    for issue in issues:
        d = serialize_issue(issue, findings.get(issue.id), active)
        if d['snoozed_until']:
            snoozed += 1
            if not include_snoozed:
                continue
        out.append(d)
    visible = [d for d in out if not d['snoozed_until']]
    summary = health.summarize(visible, snoozed)
    summary['safe_fixable'] = sum(1 for d in visible if d['fix'] and d['fix']['tier'] == health.TIER_SAFE)
    sweep = _json(_get_setting(SWEEP_KEY), {})
    summary['last_sweep_at'] = sweep.get('finished_at')
    running = _db().fetchone("SELECT id FROM tasks WHERE kind = 'health_sweep' AND status IN ('queued', 'running') "
                             "LIMIT 1")
    summary['sweep_running'] = bool(running)
    if severity:
        out = [d for d in out if d['severity'] == severity]
    if resource_type and resource_id:
        if resource_type == 'job':
            out = [d for d in out if d['resource'].get('job_id') == resource_id]
        elif resource_type == 'server':
            out = [d for d in out if d['resource'].get('type') == 'server' and d['resource'].get('id') == resource_id]
        else:
            out = [d for d in out if d['resource'].get('type') == resource_type and d['resource'].get('id') == resource_id]
    return {'summary': summary, 'issues': out}


def snooze(issue_id, hours):
    db = _db()
    row = db.fetchone('SELECT * FROM health_findings WHERE issue_id = %s', (issue_id,))
    if not row:
        return None
    until = tasks.utcnow() + timedelta(hours=hours) if hours > 0 else None
    db.execute('UPDATE health_findings SET snoozed_until = %s WHERE issue_id = %s', (until, issue_id))
    db.commit()
    return dict(row, snoozed_until=until)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _alert_channels():
    types = settings()['alert_types']
    try:
        return [c for c in _m().load_notification_channels()
                if c.get('enabled') and c.get('notify_on_failure') and c.get('type') in types]
    except Exception as e:
        logger.error(f"Health alerts: can't load channels: {e}")
        return []


def _send_alert(kind, finding):
    m = _m()
    payload = _json(finding.get('evidence'), {})
    title = finding.get('title') or payload.get('title') or finding.get('kind')
    if kind == 'new':
        status, message = 'failed', f"Critical issue: {title}\nOpen the backupx dashboard to review and fix it."
    else:
        status, message = 'recovered', f"Resolved: {title}"
    sent = False
    for channel in _alert_channels():
        try:
            m._dispatch_notification(channel, 'backupx health', status, message, 0)
            sent = True
        except Exception as e:
            logger.error(f"Health alert to {channel.get('name')} failed: {e}")
    return sent


def send_alerts(now=None):
    """Send pending critical alerts (claimed atomically, so each goes out once). Returns how many."""
    now = now or _now()
    db = _db()
    findings = [dict(r) for r in db.fetchall(
        "SELECT * FROM health_findings WHERE (resolved_at IS NULL AND severity = 'critical' AND alert "
        "AND alerted_at IS NULL) OR (resolved_at IS NOT NULL AND alerted_at IS NOT NULL "
        "AND resolved_alerted_at IS NULL)")]
    sent = 0
    for kind, f in health.alert_actions(findings, now):
        column = 'alerted_at' if kind == 'new' else 'resolved_alerted_at'
        cur = db.execute(f'UPDATE health_findings SET {column} = %s WHERE issue_id = %s AND {column} IS NULL '
                         'RETURNING issue_id', (tasks.utcnow(), f['issue_id']))
        claimed = cur.fetchone()
        db.commit()
        if not claimed:
            continue
        _send_alert(kind, f)
        sent += 1
    return sent


def alert_tick():
    """Leader, every HEALTH_ALERT_INTERVAL: re-evaluate cheap checks, sync, alert. Never raises."""
    m = _m()
    with m._short_lived_db():
        try:
            sync_findings(current_issues())
            send_alerts()
        except Exception as e:
            logger.error(f"Health alert tick failed: {e}")
            try:
                _db().rollback()
            except Exception:
                pass
        finally:
            try:
                _db().close()
            except Exception:
                pass


def schedule_sweep():
    """Leader, hourly: queue a full sweep task (any worker's runner picks it up). Never raises."""
    m = _m()
    with m._short_lived_db():
        try:
            tasks.enqueue('health_sweep', {'scope': 'all', 'scheduled': True}, title='Health check (hourly)',
                          resource_type='system', resource_id='health', idempotency_key='health_sweep:all')
        except Exception as e:
            logger.error(f"Could not queue the health sweep: {e}")
        finally:
            try:
                _db().close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Advisory locks, read-only (pg_locks): never take a lock just to test it
# ---------------------------------------------------------------------------

def advisory_lock_held(namespace, key):
    row = _db().fetchone(
        "SELECT 1 AS held FROM pg_locks WHERE locktype = 'advisory' AND classid = %s::bigint::oid "
        "AND objid = (hashtext(%s)::bigint & 4294967295)::oid AND objsubid = 2 AND granted LIMIT 1",
        (namespace, key))
    return bool(row)


def jobs_sharing_repo(url, state):
    out = []
    for jid, storages in state['job_storages'].items():
        if any(s['repo_url'] == url for s in storages):
            out.append(jid)
    return out


def backupx_using_repo(url, state, holding_repo_lock=False):
    """(held, why): a backupx advisory lock covers the repository (job run, PITR, prune, repo lock).

    holding_repo_lock: the caller holds the repository lock exclusively itself
    (so nobody else holds it, and pg_locks would only show the caller's own).
    """
    from .scheduler.locks import JOB_LOCK_NAMESPACE, REPO_LOCK_NAMESPACE, repo_lock_key
    for jid in jobs_sharing_repo(url, state):
        for key, what in ((jid, 'a backup/prune of'), (f'{jid}:pitr', 'PITR shipping of'),
                          (f'{jid}:walg-base', 'a WAL-G base backup of')):
            if advisory_lock_held(JOB_LOCK_NAMESPACE, key):
                name = state['jobs'].get(jid, {}).get('name') or jid
                return True, f'{what} {name} is running'
    if not holding_repo_lock and advisory_lock_held(REPO_LOCK_NAMESPACE, repo_lock_key(url)):
        return True, 'backupx has the repository open (backup, restore or maintenance)'
    return False, ''


def managed_hostnames(state):
    names = set()
    for sid, server in state['servers'].items():
        if server.get('host'):
            names.add(server['host'])
        probe = state['probes'].get(f'ssh:{sid}') or {}
        if (probe.get('data') or {}).get('hostname'):
            names.add(probe['data']['hostname'])
    return names


# ---------------------------------------------------------------------------
# Expensive probes
# ---------------------------------------------------------------------------

SSH_PROBE_SCRIPT = r'''
export PATH="$HOME/.local/bin:$PATH"
echo "BXH_OK"
echo "BXH_HOSTNAME $(hostname 2>/dev/null)"
if command -v restic >/dev/null 2>&1; then echo "BXH_RESTIC $(restic version 2>/dev/null | head -n1)";
else echo "BXH_RESTIC_MISSING"; fi
df -P / /var /tmp 2>/dev/null | awk 'NR>1 {print "BXH_DF", $6, $5, $4}'
exit 0
'''


def parse_ssh_probe(stdout):
    data = {'ok': False, 'hostname': None, 'restic_version': None, 'restic_missing': False, 'disks': []}
    seen = set()
    for line in (stdout or '').splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == 'BXH_OK':
            data['ok'] = True
        elif parts[0] == 'BXH_HOSTNAME' and len(parts) > 1:
            data['hostname'] = parts[1]
        elif parts[0] == 'BXH_RESTIC':
            data['restic_version'] = ' '.join(parts[1:])[:120]
        elif parts[0] == 'BXH_RESTIC_MISSING':
            data['restic_missing'] = True
        elif parts[0] == 'BXH_DF' and len(parts) >= 4 and parts[1] not in seen:
            seen.add(parts[1])
            try:
                avail = _m()._format_bytes(int(parts[3]) * 1024)
            except (ValueError, AttributeError):
                avail = parts[3]
            data['disks'].append({'mount': parts[1], 'use_pct': parts[2], 'avail': avail})
    return data


def probe_server(server):
    """SSH login + hostname + restic + disk. Stores ssh:<id>. Returns the data."""
    m = _m()
    try:
        ssh = m._build_ssh_cmd_for_server(server, timeout=10)
        proc = m._ssh_run(ssh, SSH_PROBE_SCRIPT, timeout=settings()['probe_timeout'])
        data = parse_ssh_probe(proc.stdout)
        if not data['ok']:
            err = (proc.stderr or '').strip() or f'ssh exited with code {proc.returncode}'
            data['error'] = m.sanitize_error_message(err[-800:])
            data['host_key_changed'] = health.classify_error(err) == 'host_key'
    except subprocess.TimeoutExpired:
        data = {'ok': False, 'error': 'SSH timed out'}
    except Exception as e:
        data = {'ok': False, 'error': m.sanitize_error_message(f'{type(e).__name__}: {e}')}
    save_probe(f"ssh:{server['id']}", data)
    return data


def _restic(job, target, args, timeout):
    m = _m()
    cmd = ['restic', *args]
    if target.get('skip_ssl_verify'):
        cmd.append('--insecure-tls')
    return subprocess.run(cmd, capture_output=True, text=True, env=m.job_restic_env(job, target), timeout=timeout)


def classify_open(proc):
    """restic cat config result -> ('ok' | 'not_initialized' | 'wrong_password' | 'error', message)."""
    text = ((proc.stderr or '') + ' ' + (proc.stdout or '')).strip() if proc.returncode else ''
    if proc.returncode == 0:
        return 'ok', ''
    if proc.returncode == 10:
        return 'not_initialized', text
    cls = health.classify_error(text)
    if proc.returncode == 12 or cls == 'wrong_password':
        return 'wrong_password', text
    if cls == 'not_initialized':
        return 'not_initialized', text
    return 'error', text


def list_repo_locks(job, target, timeout=60):
    """[{id, time, hostname, pid, username, exclusive}] of the repository's restic locks."""
    proc = _restic(job, target, ['list', 'locks', '--no-lock'], timeout)
    if proc.returncode != 0:
        raise RuntimeError(_m()._restic_error(proc, 'list locks'))
    locks = []
    for lock_id in [l.strip() for l in proc.stdout.splitlines() if re.fullmatch(r'[0-9a-f]{64}', l.strip())][:20]:
        p = _restic(job, target, ['cat', 'lock', lock_id, '--no-lock'], timeout)
        if p.returncode != 0:
            continue  # removed meanwhile
        try:
            data = json.loads(p.stdout)
        except ValueError:
            continue
        locks.append({'id': lock_id, 'time': data.get('time'), 'hostname': data.get('hostname'),
                      'pid': data.get('pid'), 'username': data.get('username'),
                      'exclusive': bool(data.get('exclusive'))})
    return locks


def list_repo_keys(job, target, timeout=60):
    proc = _restic(job, target, ['key', 'list', '--json', '--no-lock'], timeout)
    if proc.returncode != 0:
        raise RuntimeError(_m()._restic_error(proc, 'key list'))
    try:
        keys = json.loads(proc.stdout) or []
    except ValueError:
        raise RuntimeError('unreadable key list')
    return [{'id': k.get('id'), 'current': bool(k.get('current')), 'userName': k.get('userName'),
             'hostName': k.get('hostName'), 'created': k.get('created')} for k in keys]


def decide_locks(locks, url, state, now=None, holding_repo_lock=False):
    """Annotate locks with the stale-lock decision (read-only checks)."""
    now = now or _now()
    held, why = backupx_using_repo(url, state, holding_repo_lock)
    managed = managed_hostnames(state)
    bx_hosts = backupx_hostnames()
    min_age = settings()['stale_lock_age']
    for lock in locks:
        decision, reason = health.lock_decision(lock, now, managed, bx_hosts, held, False, min_age=min_age)
        if held and decision == 'wait':
            reason = why
        lock['decision'], lock['reason'] = decision, reason
    return locks


def probe_repo(job, target, state):
    """restic cat config, locks and keys for one job/storage. Stores repo:<job>:<storage>."""
    m = _m()
    timeout = settings()['probe_timeout']
    url = m.job_repo_url(job, target)
    data = {'open': None, 'locks': [], 'keys': []}
    try:
        proc = _restic(job, target, ['cat', 'config', '--no-lock'], timeout)
        data['open'], err = classify_open(proc)
        if err:
            data['open_error'] = m.sanitize_error_message(tasks.redact_text(err, m._job_secret_values(job, [target])))
        if data['open'] == 'ok':
            data['locks'] = decide_locks(list_repo_locks(job, target, timeout), url, state)
            data['keys'] = list_repo_keys(job, target, timeout)
    except subprocess.TimeoutExpired as e:
        tail = e.stderr.decode('utf-8', 'replace') if isinstance(e.stderr, bytes) else (e.stderr or '')
        last = [l for l in tail.splitlines() if l.strip()][-1:] or ['']
        data.update(open=data['open'] or 'error',
                    open_error=m.sanitize_error_message(tasks.redact_text(
                        f"restic timed out after {timeout}s (still retrying: {last[0]})",
                        m._job_secret_values(job, [target]))))
    except Exception as e:
        data.update(open=data['open'] or 'error', open_error=m.sanitize_error_message(str(e)))
    save_probe(f"repo:{job['id']}:{target.get('id') or ''}", data)
    return data


def check_repo(job, target):
    """restic check (structure, no data read). Stores check:<job>:<storage>."""
    from . import retention
    m = _m()
    args = ['check']
    if retention.supports_retry_lock(m._local_restic_version()):
        args += ['--retry-lock', '1m']
    try:
        proc = _restic(job, target, args, settings()['check_timeout'])
        out = tasks.redact_text(((proc.stdout or '') + '\n' + (proc.stderr or '')).strip(),
                                m._job_secret_values(job, [target]))
        tail = '\n'.join(out.splitlines()[-30:])
        if proc.returncode == 0:
            data = {'ok': True}
        elif health.classify_error(out) == 'locked':
            data = {'ok': None, 'error': 'skipped: repository locked'}
        else:
            data = {'ok': False, 'error': m.sanitize_error_message(' '.join(out.splitlines()[-3:])),
                    'output_tail': tail}
    except subprocess.TimeoutExpired:
        data = {'ok': None, 'error': 'check timed out'}
    except Exception as e:
        data = {'ok': None, 'error': str(e)[:300]}
    save_probe(f"check:{job['id']}:{target.get('id') or ''}", data)
    return data


def stats_repo(job, target):
    """restic stats --mode raw-data (expensive: daily). Stores stats:<job>:<storage>."""
    try:
        proc = _restic(job, target, ['stats', '--mode', 'raw-data', '--json', '--no-lock'],
                       settings()['check_timeout'])
        if proc.returncode != 0:
            raise RuntimeError(_m()._restic_error(proc, 'stats'))
        raw = json.loads(proc.stdout.strip().splitlines()[-1])
        data = {'bytes': raw.get('total_size'), 'files': raw.get('total_file_count'),
                'snapshots': raw.get('snapshots_count'), 'storage_name': target.get('name'),
                'job_name': job.get('name')}
    except Exception as e:
        data = {'error': str(e)[:300], 'storage_name': target.get('name'), 'job_name': job.get('name')}
    save_probe(f"stats:{job['id']}:{target.get('id') or ''}", data)
    return data


IMAGE_CHECKS = ('mysqlbinlog matches the server', 'mysqldump can record the binlog position')


def probe_prereqs(job_id, job):
    """PITR prerequisite check (MySQL/MariaDB): ok + client image mismatch. Stores prereq:<job>."""
    from . import pitr_service
    try:
        res = pitr_service.check_prereqs(job_id, job)
        data = {'ok': bool(res.get('ok')), 'kind': res.get('kind'),
                'failing': [c['check'] for c in res.get('checks', []) if c['status'] == 'fail'][:10],
                'warnings': [c['check'] for c in res.get('checks', []) if c['status'] == 'warn'][:10],
                'needs_restart': bool(res.get('needs_restart'))}
        suggested = res.get('suggested_client_image') or ''
        current = (job.get('pitr_client_image') or '').strip()
        bad = [c for c in res.get('checks', []) if c['check'] in IMAGE_CHECKS and c['status'] in ('fail', 'warn')]
        if bad and suggested and suggested != current and res.get('docker'):
            data['image_mismatch'] = {'current': current, 'suggested': suggested,
                                      'server_version': res.get('server_version'),
                                      'reason': bad[0].get('current')}
    except Exception as e:
        data = {'ok': None, 'error': str(e)[:300]}
    save_probe(f'prereq:{job_id}', data)
    return data


def _targets_for(job):
    return _m().get_job_storage_targets(job)


def sweep(ctx=None, resource_type=None, resource_id=None, force_expensive=False):
    """Run the expensive probes (all, or one job/server/storage). Returns a summary dict."""
    m = _m()
    cfg = settings()
    state = collect_state()
    now = _now()
    probes = state['probes']
    log = ctx.log if ctx else (lambda *a, **k: None)
    jobs = state['jobs']
    if resource_type == 'job':
        jobs = {k: v for k, v in jobs.items() if k == resource_id}
        server_ids = {j.get('server_id') for j in jobs.values() if j.get('server_id')}
    elif resource_type == 'server':
        server_ids = {resource_id}
        jobs = {}
    elif resource_type == 'storage':
        jobs = {k: v for k, v in jobs.items()
                if any((s.get('id') or '') == resource_id for s in state['job_storages'].get(k, []))}
        server_ids = set()
    else:
        server_ids = set(state['servers'])
    servers = [state['servers'][s] for s in server_ids if s in state['servers']]
    work = [('server', s) for s in servers]
    for jid, job in jobs.items():
        for target in _targets_for(job):
            if resource_type == 'storage' and (target.get('id') or '') != resource_id:
                continue
            work.append(('repo', (job, target)))
        if job.get('pitr_enabled') and job.get('backup_type') == 'database':
            kind = (state['pitr'].get(jid) or {}).get('kind') or 'mysql'
            if kind == 'mysql':
                work.append(('prereq', (jid, job)))
    total = max(1, len(work))
    done = [0]
    lock = threading.Lock()

    def step(label):
        with lock:
            done[0] += 1
            if ctx:
                ctx.progress(int(done[0] * 90 / total), label)

    def run(item):
        what, obj = item
        if ctx and ctx.cancelled:
            return
        try:
            if what == 'server':
                data = probe_server(obj)
                log(f"server {obj.get('name')}: {'ok' if data.get('ok') else 'FAILED - ' + str(data.get('error'))}")
                step(f"Checked server {obj.get('name')}")
            elif what == 'repo':
                job, target = obj
                if ctx:
                    ctx.add_secret(*m._job_secret_values(job, [target]))
                data = probe_repo(job, target, state)
                log(f"{job.get('name')} / {target.get('name')}: open={data.get('open')} "
                    f"locks={len(data.get('locks') or [])} keys={len(data.get('keys') or [])}")
                sid = target.get('id') or ''
                if data.get('open') == 'ok':
                    ck = probes.get(f"check:{job['id']}:{sid}")
                    if force_expensive or health.is_due(ck and ck['checked_at'], now, cfg['check_days'] * 86400):
                        res = check_repo(job, target)
                        log(f"  restic check: {res.get('ok')} {res.get('error') or ''}")
                    st = probes.get(f"stats:{job['id']}:{sid}")
                    if force_expensive or health.is_due(st and st['checked_at'], now, cfg['stats_hours'] * 3600):
                        res = stats_repo(job, target)
                        log(f"  stats: {res.get('bytes')} bytes")
                step(f"Checked {job.get('name')} on {target.get('name')}")
            elif what == 'prereq':
                jid, job = obj
                data = probe_prereqs(jid, job)
                log(f"{job.get('name')}: PITR prerequisites ok={data.get('ok')}")
                step(f"Checked PITR prerequisites of {job.get('name')}")
        except Exception as e:
            log(f"{what}: check failed: {e}", stream='stderr')
        finally:
            try:
                _db().close()
            except Exception:
                pass

    db_backend = _db()
    scope = getattr(db_backend, 'short_lived_connections', None)

    def run_scoped(item):
        if scope is None:
            return run(item)
        with scope():
            return run(item)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='health') as pool:
        list(pool.map(run_scoped, work))
    if ctx:
        ctx.check_cancelled()
    # Probes of deleted jobs/servers
    if resource_type is None:
        valid = {f'ssh:{s}' for s in state['servers']} | {f'prereq:{j}' for j in state['jobs']} \
            | {f'pitrplan:{j}' for j in state['jobs']} | {f'drill:{j}' for j in state['jobs']}
        for jid, storages in state['job_storages'].items():
            for s in storages:
                for p in ('repo', 'check', 'stats'):
                    valid.add(f"{p}:{jid}:{s.get('id') or ''}")
        db = _db()
        for key in list(load_probes()):
            if key not in valid:
                db.execute('DELETE FROM health_probes WHERE key = %s', (key,))
        db.commit()
    issues = current_issues()
    sync_findings(issues)
    if resource_type is None:
        _set_setting(SWEEP_KEY, json.dumps({'finished_at': _now().isoformat(), 'checked': len(work)}))
    try:
        send_alerts()
    except Exception as e:
        logger.error(f"Health alerts after sweep failed: {e}")
    return {'checked': len(work), 'issues': len(issues),
            'critical': sum(1 for i in issues if i.severity == health.SEV_CRITICAL)}


# ---------------------------------------------------------------------------
# Fix catalogue
# ---------------------------------------------------------------------------

class NotApplicable(Exception):
    """The fix doesn't apply to the current state (the issue changed or went away)."""


class Fix:
    def __init__(self, action, tier, label, description, order=50, confirm_phrase=None):
        self.action = action
        self.tier = tier
        self.label = label
        self.description = description
        self.order = order                 # fix-all waves: lower first
        self.confirm_phrase = confirm_phrase  # fn(issue, plan) -> phrase to type (confirm tier)
        self.plan_fn = None
        self.exec_fn = None

    def plan(self, issue):
        return self.plan_fn(issue)

    def confirm_text(self, issue, plan):
        if self.tier != health.TIER_CONFIRM:
            return None
        if self.confirm_phrase:
            return self.confirm_phrase(issue, plan)
        return issue.resource.get('job_name') or issue.resource.get('name')


FIXES = {}


def fix(action, tier, label, description, order=50, confirm_phrase=None):
    def wrap(plan_fn):
        f = Fix(action, tier, label, description, order, confirm_phrase)
        f.plan_fn = plan_fn
        FIXES[action] = f
        return plan_fn
    return wrap


def executes(action):
    def wrap(fn):
        FIXES[action].exec_fn = fn
        return fn
    return wrap


def _plan(action, title, steps, downtime='None', details=None, warnings=None):
    out = {'action': action, 'title': title, 'steps': list(steps), 'downtime': downtime,
           'details': dict(details or {})}
    if warnings:
        out['warnings'] = list(warnings)
    return out


def _job_of(issue):
    m = _m()
    job_id = issue.resource.get('job_id')
    job = m.get_job(job_id) if job_id else None
    if not job:
        raise NotApplicable('the job no longer exists')
    return dict(job, id=job_id)


def _target_of(job, issue):
    sid = issue.resource.get('storage_id')
    for t in _m().get_job_storage_targets(job):
        if (t.get('id') or '') == (sid or ''):
            return t
    raise NotApplicable('the storage is no longer an active storage of the job')


def _failed_storage_ids(issue, job):
    names = {n.strip() for n in (issue.evidence.get('failed_storages') or '').split(',') if n.strip()}
    ids = [t.get('id') for t in _m().get_job_storage_targets(job) if t.get('name') in names and t.get('id')]
    return sorted(ids)


def _run_backup_in_task(ctx, job_id, storage_ids=None, trigger='fix'):
    ok, message = _m().run_backup(job_id, storage_ids=storage_ids or None, trigger=trigger, task=ctx)
    ctx.log(message, stream='system')
    if not ok:
        if message.startswith('Skipped'):
            raise tasks.TaskBusy(message)
        raise tasks.TaskError(message)
    return message


# --- safe ---------------------------------------------------------------------

@fix('run_now', health.TIER_SAFE, 'Run backup now', 'Starts a backup of this job now.', order=60)
def _plan_run_now(issue):
    job = _job_of(issue)
    return _plan('run_now', f"Run {job.get('name')} now",
                 ['Start a backup of the job on all its active storages'],
                 details={'job': job.get('name'), 'due_at': issue.evidence.get('due_at')})


@executes('run_now')
def _exec_run_now(ctx, plan, issue):
    job = _job_of(issue)
    return {'message': _run_backup_in_task(ctx, job['id'])}


@fix('rerun_failed', health.TIER_SAFE, 'Retry failed storages',
     'Runs the backup again on the failed storages only.', order=50)
def _plan_rerun(issue):
    job = _job_of(issue)
    ids = _failed_storage_ids(issue, job)
    names = issue.evidence.get('failed_storages')
    if not ids and job.get('s3_config_ids'):
        raise NotApplicable('none of the failed storages is an active storage of the job anymore')
    return _plan('rerun_failed', f"Retry {job.get('name')} on {names}",
                 [f'Back up the job again on: {names}'],
                 details={'job': job.get('name'), 'storages': names, 'storage_ids': ids,
                          'run_id': issue.evidence.get('run_id')})


@executes('rerun_failed')
def _exec_rerun(ctx, plan, issue):
    ids = plan['details'].get('storage_ids') or None
    return {'message': _run_backup_in_task(ctx, issue.resource['job_id'], ids)}


@fix('storage_retry', health.TIER_SAFE, 'Re-test and retry',
     'Re-tests the storage and retries the failed storages with a back-off.', order=45)
def _plan_storage_retry(issue):
    plan = _plan_rerun(issue)
    backoff = settings()['retry_backoff']
    plan.update(action='storage_retry', title=plan['title'].replace('Retry', 'Re-test and retry', 1),
                steps=['Test that each failed storage endpoint answers',
                       f'Retry the backup on the failed storages, up to {len(backoff) + 1} attempts '
                       f'(waiting {", ".join(str(b) + " s" for b in backoff)} between them)'])
    return plan


@executes('storage_retry')
def _exec_storage_retry(ctx, plan, issue):
    m = _m()
    job = _job_of(issue)
    ids = plan['details'].get('storage_ids') or []
    targets = [t for t in m.get_job_storage_targets(job) if not ids or t.get('id') in ids]
    for t in targets:
        kind, message = m.probe_s3_endpoint(t['endpoint'], t.get('skip_ssl_verify'))
        ctx.log(f"{t['name']}: endpoint test: {kind}{' - ' + message if message else ''}")
    backoff = settings()['retry_backoff']
    last = None
    for attempt in range(len(backoff) + 1):
        ctx.step(attempt + 1, len(backoff) + 1, f'Backup attempt {attempt + 1}')
        try:
            return {'message': _run_backup_in_task(ctx, job['id'], ids or None), 'attempts': attempt + 1}
        except tasks.TaskBusy:
            raise
        except tasks.TaskError as e:
            last = e
            # Only the storages that still fail
            latest = {r['storage_id']: r for r in _db().fetchall(
                'SELECT DISTINCT ON (storage_id) storage_id, status FROM run_results WHERE job_id = %s '
                'ORDER BY storage_id, finished_at DESC NULLS LAST, id DESC', (job['id'],))}
            ids = [i for i in ids if (latest.get(i) or {}).get('status') != 'success'] or ids
            if attempt < len(backoff):
                ctx.log(f'Attempt {attempt + 1} failed; retrying in {backoff[attempt]} s', stream='system')
                if ctx.cancel_event.wait(backoff[attempt]):
                    raise tasks.TaskCancelled()
    raise tasks.TaskError(f'Still failing after {len(backoff) + 1} attempts: {last}')


@fix('init_repo', health.TIER_SAFE, 'Create repository',
     'Initializes the missing restic repository on this storage.', order=30)
def _plan_init(issue):
    job = _job_of(issue)
    target = _target_of(job, issue)
    proc = _restic(job, target, ['cat', 'config', '--no-lock'], settings()['probe_timeout'])
    state, _ = classify_open(proc)
    if state != 'not_initialized' or proc.returncode != 10:
        # Only restic's own "does not exist" (exit 10) is a reason to init
        raise NotApplicable('restic no longer reports the repository as missing (exit code 10)')
    return _plan('init_repo', f"Create the repository of {job.get('name')} on {target.get('name')}",
                 ['restic init on the storage (the job\'s password becomes its key)'],
                 details={'job': job.get('name'), 'storage': target.get('name')})


@executes('init_repo')
def _exec_init(ctx, plan, issue):
    m = _m()
    job = _job_of(issue)
    target = _target_of(job, issue)
    ctx.add_secret(*m._job_secret_values(job, [target]))
    with ctx.lock(job['id'], wait=300, what='This job (a backup is running)'):
        with ctx.repo_locks([m.job_repo_url(job, target)]):
            outcome = m._init_job_repos(job, [target], log=ctx.log)
    probe_repo(job, target, collect_state())
    if not outcome['success']:
        raise tasks.TaskError(outcome['error'])
    return {'message': outcome['message']}


def _live_lock(issue):
    """Re-read the lock named by the issue: (job, target, url, lock dict) or NotApplicable."""
    m = _m()
    job = _job_of(issue)
    target = _target_of(job, issue)
    lock_id_prefix = issue.evidence.get('lock_id') or ''
    try:
        locks = list_repo_locks(job, target, settings()['probe_timeout'])
    except Exception as e:
        raise NotApplicable(f"can't list the repository's locks: {e}")
    lock = next((l for l in locks if l['id'].startswith(lock_id_prefix)), None) if lock_id_prefix else None
    if lock is None:
        raise NotApplicable('the lock is gone')
    url = m.job_repo_url(job, target)
    decide_locks([lock], url, collect_state())
    return job, target, url, lock


def _lock_plan(action, issue, want):
    job, target, url, lock = _live_lock(issue)
    if lock['decision'] != want:
        raise NotApplicable(f"the lock can't be removed this way now: {lock['reason']}")
    server = server_for_host(collect_state(), lock.get('hostname'))
    where = (f"on {server.get('name')} (the lock's own host, so restic also checks pid {lock.get('pid')} is gone)"
             if server else 'from the backupx container')
    steps = ['Check again that no backupx run uses the repository and hold it exclusively',
             f'restic unlock {where}: removes only locks restic itself considers stale; never --remove-all',
             'List the locks again to confirm it is gone']
    return _plan(action, f"Remove the stale restic lock on {target.get('name')} ({job.get('name')})", steps,
                 downtime='None (backups of this repository wait a few seconds)',
                 details={'job': job.get('name'), 'storage': target.get('name'), 'lock_id': lock['id'][:12],
                          'host': lock.get('hostname'), 'pid': lock.get('pid'), 'user': lock.get('username'),
                          'created': lock.get('time'), 'exclusive': lock.get('exclusive')},
                 warnings=None if want == 'auto' else [
                     f"{lock.get('hostname') or 'This host'} is not a server backupx manages. Make sure no restic "
                     f"process (pid {lock.get('pid')}) is still running there."])


def server_for_host(state, hostname):
    """The managed server whose hostname (as probed) or address is `hostname`, or None."""
    name = (hostname or '').strip().lower()
    if not name:
        return None
    for sid, server in state['servers'].items():
        probe = (state['probes'].get(f'ssh:{sid}') or {}).get('data') or {}
        for candidate in (probe.get('hostname'), server.get('host')):
            c = (candidate or '').strip().lower()
            if c and (c == name or c.split('.')[0] == name.split('.')[0]):
                return server
    return None


def _unlock_script(job, target):
    """restic unlock on a server; secrets are exported inside the script (stdin), never argv."""
    import shlex
    m = _m()
    return f"""
export PATH="$HOME/.local/bin:$PATH"
export AWS_ACCESS_KEY_ID={shlex.quote(target.get('access_key') or '')}
export AWS_SECRET_ACCESS_KEY={shlex.quote(target.get('secret_key') or '')}
export RESTIC_REPOSITORY={shlex.quote(m.job_repo_url(job, target))}
export RESTIC_PASSWORD={shlex.quote(job.get('restic_password') or '')}
restic unlock {'--insecure-tls' if target.get('skip_ssl_verify') else ''} 2>&1
"""


def _restic_unlock(ctx, job, target, lock, state):
    """restic unlock (never --remove-all) on the lock's own host when backupx manages it, else here.

    On the lock's host restic also sees that the lock's process is gone; from
    anywhere else it only removes locks older than 30 minutes.
    """
    m = _m()
    server = server_for_host(state, lock.get('hostname'))
    if server:
        ctx.log(f"restic unlock on {server.get('name')} (the lock's host)")
        try:
            proc = m._ssh_run(m._build_ssh_cmd_for_server(server), _unlock_script(job, target),
                              timeout=settings()['probe_timeout'])
            for line in ((proc.stdout or '') + (proc.stderr or '')).splitlines():
                ctx.log(line)
            if proc.returncode == 0:
                return
            ctx.log(f'unlock over SSH failed (exit {proc.returncode}); trying from the backupx container',
                    stream='stderr')
        except Exception as e:
            ctx.log(f'unlock over SSH failed ({e}); trying from the backupx container', stream='stderr')
    ctx.log('restic unlock from the backupx container')
    proc = _restic(job, target, ['unlock'], settings()['probe_timeout'])
    for line in ((proc.stdout or '') + (proc.stderr or '')).splitlines():
        ctx.log(line)
    if proc.returncode != 0:
        raise tasks.TaskError(m._restic_error(proc, 'unlock'))


def _do_unlock(ctx, issue, plan, want):
    m = _m()
    job, target, url, lock = _live_lock(issue)
    ctx.add_secret(*m._job_secret_values(job, [target]))
    with ctx.repo_locks([url], exclusive=True):
        state = collect_state()
        decide_locks([lock], url, state, holding_repo_lock=True)
        if lock['decision'] != want:
            raise tasks.TaskError(f"Not unlocking: {lock['reason']}")
        ctx.log(f"Removing lock {lock['id'][:12]} on {target.get('name')} (from {lock.get('hostname')}, "
                f"pid {lock.get('pid')})")
        _restic_unlock(ctx, job, target, lock, state)
        remaining = [l for l in list_repo_locks(job, target) if l['id'] == lock['id']]
    probe_repo(job, target, collect_state())
    if remaining:
        raise tasks.TaskError('restic unlock ran but the lock is still there (restic does not consider it stale)')
    return {'message': f"Removed the stale lock on {target.get('name')}"}


@fix('unlock_stale', health.TIER_SAFE, 'Remove stale lock',
     'Removes a restic lock left by a dead process (safe: old, from a managed host, repository idle).', order=10)
def _plan_unlock_stale(issue):
    return _lock_plan('unlock_stale', issue, 'auto')


@executes('unlock_stale')
def _exec_unlock_stale(ctx, plan, issue):
    return _do_unlock(ctx, issue, plan, 'auto')


@fix('retry_forget', health.TIER_SAFE, 'Retry retention',
     'Removes a stale lock if that is safe, then runs restic forget again.', order=70)
def _plan_retry_forget(issue):
    from . import retention
    job = _job_of(issue)
    target = _target_of(job, issue)
    cfg = retention.settings()
    if not cfg['enabled']:
        raise NotApplicable('retention is switched off (RETENTION_ENABLED=false)')
    if not retention.retention_policy(job):
        raise NotApplicable('the job has no keep rules')
    return _plan('retry_forget', f"Retry retention of {job.get('name')} on {target.get('name')}",
                 ['If the repository has a stale lock that is safe to remove, remove it',
                  f"restic forget with the job's policy ({retention.describe_policy(job)})" +
                  (' - dry run only' if cfg['dry_run'] else '')],
                 details={'job': job.get('name'), 'storage': target.get('name'),
                          'policy': retention.describe_policy(job), 'dry_run': cfg['dry_run']})


@executes('retry_forget')
def _exec_retry_forget(ctx, plan, issue):
    from . import retention
    m = _m()
    job = _job_of(issue)
    target = _target_of(job, issue)
    cfg = retention.settings()
    ctx.add_secret(*m._job_secret_values(job, [target]))
    url = m.job_repo_url(job, target)
    state = collect_state()
    try:
        locks = decide_locks(list_repo_locks(job, target), url, state)
    except Exception as e:
        locks = []
        ctx.log(f"Can't list locks: {e}", stream='stderr')
    stale = [l for l in locks if l['decision'] == 'auto']
    if stale:
        with ctx.repo_locks([url], exclusive=True):
            state = collect_state()
            for lock in decide_locks(stale, url, state, holding_repo_lock=True):
                if lock['decision'] != 'auto':
                    continue
                ctx.log(f"Removing stale lock {lock['id'][:12]} from {lock.get('hostname')} first")
                try:
                    _restic_unlock(ctx, job, target, lock, state)
                except tasks.TaskError as e:
                    ctx.log(str(e), stream='stderr')
    with ctx.lock(job['id'], wait=600, what='This job (a backup is running)'):
        with ctx.repo_locks([url]):
            shared = m._jobs_sharing_repo(job['id'], job, target)
            if shared:
                raise tasks.TaskError(f"Repository shared with {', '.join(shared)}: retention skips it")
            try:
                removed, message = m.forget_on_target(job, target, cfg)
            except Exception as e:
                message = m.sanitize_error_message(f"Retention (forget) failed on {target.get('name')}: {e}")
                m.record_retention_run(job['id'], target, 'forget', 'failed', message, dry_run=cfg['dry_run'])
                raise tasks.TaskError(message)
    m.record_retention_run(job['id'], target, 'forget', 'success', message, snapshots_removed=removed,
                           dry_run=cfg['dry_run'])
    m.invalidate_snapshot_cache(job['id'])
    ctx.log(message)
    return {'message': message}


@fix('install_restic', health.TIER_SAFE, 'Install restic',
     'Installs the pinned static restic binary on the server over SSH.', order=20)
def _plan_install_restic(issue):
    server = _m().get_server(issue.resource.get('server_id') or issue.resource.get('id'))
    if not server:
        raise NotApplicable('the server no longer exists')
    return _plan('install_restic', f"Install restic on {server.get('name')}",
                 [f"Download restic {_m().RESTIC_VERSION} (static binary) to /usr/local/bin or ~/.local/bin"],
                 details={'server': server.get('name'), 'host': server.get('host')})


@executes('install_restic')
def _exec_install_restic(ctx, plan, issue):
    m = _m()
    ctx.params = dict(ctx.params, server_id=issue.resource.get('server_id') or issue.resource.get('id'))
    result = m._task_install_restic(ctx)
    server = m.get_server(ctx.params['server_id'])
    if server:
        probe_server(server)
    return result


def _pitr_tick_forced(ctx, job_id, reset_walg=False):
    from . import pitr_service
    with ctx.lock(pitr_service.lock_key(job_id), wait=300, what='PITR shipping of this job'):
        if reset_walg:
            state = pitr_service.load_state(job_id)
            info = _json(state.get('info'), {})
            info.pop('walg_env_hash', None)
            pitr_service.save_state(job_id, info=json.dumps(info))
        try:
            ok, message = pitr_service._tick(job_id, force=True)
        except Exception as e:
            pitr_service._record_failure(job_id, f'{type(e).__name__}: {e}')
            raise tasks.TaskError(_m().sanitize_error_message(str(e)))
    ctx.log(message)
    if not ok:
        raise tasks.TaskError(message)
    return message


@fix('pitr_kick', health.TIER_SAFE, 'Restart streamer',
     'Re-installs and restarts the binlog streamer, then ships what is queued.', order=80)
def _plan_pitr_kick(issue):
    job = _job_of(issue)
    if not job.get('pitr_enabled'):
        raise NotApplicable('PITR is off for this job')
    return _plan('pitr_kick', f"Restart the binlog streamer of {job.get('name')}",
                 ['Re-write the streamer unit, script and credentials on the server',
                  'Restart backupx-pitr-binlog@<job> (systemd)', 'Ship the queued binlogs'],
                 downtime='None (the database is not touched)', details={'job': job.get('name')})


@executes('pitr_kick')
def _exec_pitr_kick(ctx, plan, issue):
    from . import pitr, pitr_service
    job_id = issue.resource['job_id']
    message = _pitr_tick_forced(ctx, job_id)
    # The restarted streamer touches its heartbeat once it has caught up (its
    # watchdog checks every minute): wait for that so the fix ends verified.
    max_age = pitr.settings()['heartbeat_max_age']
    for attempt in range(6):
        state = pitr_service.load_state(job_id)
        if (state.get('kind') or 'mysql') != 'mysql':
            break
        age = health.age_seconds(state.get('heartbeat'), _now())
        if state.get('streamer_active') == 'active' and age is not None and age <= max_age:
            ctx.log(f'Streamer active, heartbeat {health.human_age(age)} old')
            return {'message': message, 'verified': True}
        ctx.log(f"Waiting for the streamer's heartbeat (state {state.get('streamer_active')}, "
                f"heartbeat {health.human_age(age)} old)", stream='system')
        if ctx.cancel_event.wait(20):
            raise tasks.TaskCancelled()
        with ctx.lock(pitr_service.lock_key(job_id), wait=120, what='PITR shipping of this job'):
            ok, message = pitr_service._tick(job_id)
    else:
        raise tasks.TaskError('The streamer was restarted but has not reported a fresh heartbeat yet; '
                              'check its journal (journalctl -u backupx-pitr-binlog@<job>)')
    return {'message': message}


@fix('walg_repush', health.TIER_SAFE, 'Repair WAL archiving',
     'Re-installs the wal-g environment and archive script, then tests the storage.', order=80)
def _plan_walg(issue):
    job = _job_of(issue)
    return _plan('walg_repush', f"Re-install WAL-G archiving files for {job.get('name')}",
                 ['Re-write /etc/backupx-pitr env and /usr/local/bin/wal-g-archive.sh on the server',
                  'Check the archiver status (pg_stat_archiver)', 'Test the storage with wal-g st ls'],
                 downtime='None (PostgreSQL is not restarted)', details={'job': job.get('name')})


@executes('walg_repush')
def _exec_walg(ctx, plan, issue):
    from . import pitr_scripts, pitr_service
    m = _m()
    message = _pitr_tick_forced(ctx, issue.resource['job_id'], reset_walg=True)
    job, server, _db_config, _kind = pitr_service._job_context(issue.resource['job_id'])
    res = m._ssh_run(m._build_ssh_cmd_for_server(server), pitr_scripts.walg_run_script(
        'wal-g st ls >/dev/null && echo WALG_STORAGE_OK'), timeout=120)
    if 'WALG_STORAGE_OK' not in res.stdout:
        raise tasks.TaskError('wal-g cannot reach the storage: ' + m.sanitize_error_message(res.stderr.strip()[-300:]))
    ctx.log('wal-g storage test: ok')
    return {'message': message}


@fix('wal_mirror_retry', health.TIER_SAFE, 'Retry WAL copy', 'Copies WAL to the other storages again now.', order=85)
def _plan_mirror(issue):
    job = _job_of(issue)
    if len(_m().get_job_storage_targets(job)) < 2:
        raise NotApplicable('the job has a single storage')
    return _plan('wal_mirror_retry', f"Copy WAL of {job.get('name')} to the other storages",
                 ['rclone copy (never sync) of the WAL-G prefix from the primary storage to the others'],
                 details={'job': job.get('name')})


@executes('wal_mirror_retry')
def _exec_mirror(ctx, plan, issue):
    from . import pitr_service
    m = _m()
    job = _job_of(issue)
    targets = m.get_job_storage_targets(job)
    with ctx.lock(pitr_service.lock_key(job['id']), wait=300, what='PITR shipping of this job'):
        result = pitr_service._mirror_wal(job.get('backup_prefix') or job['id'], targets[0], targets[1:])
        state = pitr_service.load_state(job['id'])
        info = _json(state.get('info'), {})
        info.update(mirror=result, mirror_at=_now().isoformat())
        pitr_service.save_state(job['id'], info=json.dumps(info))
    failed = [f"{r['storage']}: {r['error']}" for r in result if not r['ok']]
    if failed:
        raise tasks.TaskError('WAL copy failed - ' + '; '.join(failed))
    return {'message': f'WAL copied to {len(result)} storage(s)'}


@fix('ssh_retest', health.TIER_SAFE, 'Re-test SSH', 'Tries to log in to the server again.', order=5)
def _plan_ssh_retest(issue):
    server = _m().get_server(issue.resource.get('id'))
    if not server:
        raise NotApplicable('the server no longer exists')
    return _plan('ssh_retest', f"Re-test SSH to {server.get('name')}", ['Log in over SSH and run a short probe'],
                 details={'server': server.get('name'), 'host': server.get('host')})


@executes('ssh_retest')
def _exec_ssh_retest(ctx, plan, issue):
    server = _m().get_server(issue.resource.get('id'))
    data = probe_server(server)
    if not data.get('ok'):
        raise tasks.TaskError(f"Still can't connect: {data.get('error')}")
    return {'message': f"SSH to {server.get('name')} works again"}


@fix('recheck_repo', health.TIER_SAFE, 'Re-check', 'Opens the repository again.', order=6)
def _plan_recheck_repo(issue):
    job = _job_of(issue)
    target = _target_of(job, issue)
    return _plan('recheck_repo', f"Re-check {job.get('name')} on {target.get('name')}",
                 ['Open the repository (restic cat config) and list its locks and keys'],
                 details={'job': job.get('name'), 'storage': target.get('name')})


@executes('recheck_repo')
def _exec_recheck_repo(ctx, plan, issue):
    job = _job_of(issue)
    target = _target_of(job, issue)
    data = probe_repo(job, target, collect_state())
    if data.get('open') != 'ok':
        raise tasks.TaskError(f"Still can't open it: {data.get('open_error') or data.get('open')}")
    return {'message': 'The repository opens fine'}


@fix('notification_test', health.TIER_SAFE, 'Send test message', 'Sends a test notification to the channel.', order=90)
def _plan_notify_test(issue):
    channel = _m().get_notification_channel(issue.resource.get('id'))
    if not channel:
        raise NotApplicable('the channel no longer exists')
    return _plan('notification_test', f"Send a test message to {channel.get('name')}",
                 ['Send one test notification'], details={'channel': channel.get('name'), 'type': channel.get('type')})


@executes('notification_test')
def _exec_notify_test(ctx, plan, issue):
    m = _m()
    channel = m.get_notification_channel(issue.resource.get('id'))
    try:
        m._dispatch_notification(channel, 'backupx', 'success', 'Test notification from the backupx health check', 0)
    except Exception as e:
        record_channel_result(channel['id'], e, _channel_secrets(channel))
        raise tasks.TaskError(f'Sending failed: {tasks.redact_text(str(e), _channel_secrets(channel))}')
    record_channel_result(channel['id'])
    return {'message': f"Test message sent to {channel.get('name')}"}


def _channel_secrets(channel):
    cfg = channel.get('config') or {}
    return [str(v) for k, v in cfg.items() if v and isinstance(v, str) and
            any(s in k.lower() for s in ('token', 'password', 'secret', 'url', 'webhook'))]


# --- confirm --------------------------------------------------------------------

@fix('unlock_lock', health.TIER_CONFIRM, 'Review lock removal',
     "Removes an old restic lock from a host backupx doesn't manage, after you confirm.", order=10)
def _plan_unlock_lock(issue):
    return _lock_plan('unlock_lock', issue, 'confirm')


@executes('unlock_lock')
def _exec_unlock_lock(ctx, plan, issue):
    return _do_unlock(ctx, issue, plan, 'confirm')


@fix('restore_drill', health.TIER_SAFE, 'Run the drill again',
     'Restores the job into a scratch copy on the backupx server and compares it with production.', order=85)
def _plan_restore_drill(issue):
    job = _job_of(issue)
    if not job.get('pitr_enabled'):
        raise NotApplicable('PITR is off for this job')
    return _plan('restore_drill', f"Restore drill of {job.get('name')}",
                 ['Checksum production (read-only)', 'Scratch restore to a recent second on the backupx server',
                  'Checksum the copy and production again, compare quiet tables', 'Remove the copy'],
                 downtime='None (reads production; the copy runs on the backupx server)',
                 details={'job': job.get('name')})


@executes('restore_drill')
def _exec_restore_drill(ctx, plan, issue):
    from . import drill_service
    job_id = issue.resource['job_id']
    with ctx.lock(f'{job_id}:drill', wait=60, what='Another drill of this job'):
        result = drill_service.run_drill(ctx, job_id)
    if not result['ok']:
        raise tasks.TaskError(result['summary'])
    return {'message': result['summary']}


@fix('anchored_dump', health.TIER_CONFIRM, 'Review anchored backup',
     'Takes a full database backup now that anchors point-in-time recovery.', order=90)
def _plan_anchored(issue):
    from . import pitr_service
    job = _job_of(issue)
    try:
        _job, _server, db_config, kind = pitr_service._job_context(job['id'], job)
    except Exception as e:
        raise NotApplicable(str(e))
    pre = pitr_service.check_prereqs(job['id'], job)
    if not pre.get('ok'):
        failing = [c['check'] for c in pre.get('checks', []) if c['status'] == 'fail']
        raise NotApplicable('prerequisites fail: ' + ', '.join(failing))
    if kind == 'postgres':
        steps = ['wal-g backup-push of the data directory (a base backup) on the database server']
    else:
        steps = ['Full mysqldump with --single-transaction and the binlog position (--source-data=2)',
                 'Store the dump on every active storage', 'Record it as the PITR anchor']
    return _plan('anchored_dump', f"Anchored full backup of {job.get('name')} now", steps,
                 downtime='No downtime; extra load on the database while the dump runs',
                 details={'job': job.get('name'), 'database': db_config.get('name'), 'kind': kind,
                          'prerequisites': 'ok'})


@executes('anchored_dump')
def _exec_anchored(ctx, plan, issue):
    from . import pitr_service
    job_id = issue.resource['job_id']
    if plan['details'].get('kind') == 'postgres':
        ok, message = pitr_service.run_pg_base_backup(job_id)
        if not ok:
            raise tasks.TaskError(message)
    else:
        message = _run_backup_in_task(ctx, job_id, trigger='fix')
    return {'message': message}


@fix('remove_key', health.TIER_CONFIRM, 'Review key removal', 'Removes the extra restic key from the repository.',
     order=95, confirm_phrase=lambda issue, plan: plan['details'].get('key_id'))
def _plan_remove_key(issue):
    job = _job_of(issue)
    target = _target_of(job, issue)
    prefix = issue.evidence.get('key_id') or ''
    try:
        keys = list_repo_keys(job, target)
    except Exception as e:
        raise NotApplicable(f"can't list keys: {e}")
    key = next((k for k in keys if prefix and (k['id'] or '').startswith(prefix)), None)
    if key is None:
        raise NotApplicable('the key is gone')
    if key['current']:
        raise NotApplicable("that is the job's own key")
    if not any(k['current'] for k in keys):
        raise NotApplicable("the job's own key isn't in the repository")
    return _plan('remove_key', f"Remove restic key {key['id'][:8]} from {target.get('name')}",
                 [f"restic key remove {key['id'][:8]}", "The job's own key stays"],
                 details={'job': job.get('name'), 'storage': target.get('name'), 'key_id': key['id'][:8],
                          'user': key.get('userName'), 'host': key.get('hostName'), 'created': key.get('created')},
                 warnings=['Anyone who uses this password to read the repository loses access.'])


@executes('remove_key')
def _exec_remove_key(ctx, plan, issue):
    m = _m()
    job = _job_of(issue)
    target = _target_of(job, issue)
    ctx.add_secret(*m._job_secret_values(job, [target]))
    keys = list_repo_keys(job, target)
    key = next((k for k in keys if k['id'].startswith(plan['details']['key_id'])), None)
    if key is None or key['current']:
        raise tasks.TaskError('The key changed; nothing removed')
    with ctx.repo_locks([m.job_repo_url(job, target)]):
        proc = _restic(job, target, ['key', 'remove', key['id']], settings()['probe_timeout'])
    if proc.returncode != 0:
        raise tasks.TaskError(m._restic_error(proc, 'key remove'))
    probe_repo(job, target, collect_state())
    return {'message': f"Removed key {key['id'][:8]}"}


@fix('repair_index', health.TIER_CONFIRM, 'Review index repair',
     'Rebuilds the repository index (restic repair index), then checks again.', order=96)
def _plan_repair(issue):
    job = _job_of(issue)
    target = _target_of(job, issue)
    return _plan('repair_index', f"Repair the index of {job.get('name')} on {target.get('name')}",
                 ['Wait for running backups of the job to finish and hold the job and the repository exclusively',
                  'restic repair index', 'restic check (structure) to confirm'],
                 downtime="Backups of this job are skipped while it runs (minutes to an hour for large repositories)",
                 details={'job': job.get('name'), 'storage': target.get('name'),
                          'check_error': issue.evidence.get('error')})


@executes('repair_index')
def _exec_repair(ctx, plan, issue):
    m = _m()
    job = _job_of(issue)
    target = _target_of(job, issue)
    ctx.add_secret(*m._job_secret_values(job, [target]))
    with ctx.lock(job['id'], wait=1800, what='This job (a backup is running)'):
        with ctx.repo_locks([m.job_repo_url(job, target)], exclusive=True):
            ctx.step(1, 2, 'restic repair index')
            cmd = ['restic', 'repair', 'index'] + (['--insecure-tls'] if target.get('skip_ssl_verify') else [])
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=m.job_restic_env(job, target))
            rc = ctx.stream(proc, timeout=settings()['check_timeout'])
            if rc != 0:
                raise tasks.TaskError(f'restic repair index exited with code {rc}')
            ctx.step(2, 2, 'restic check')
            result = check_repo(job, target)
    if result.get('ok') is False:
        raise tasks.TaskError(f"The check still fails: {result.get('error')}")
    return {'message': 'Index rebuilt; restic check passes' if result.get('ok') else 'Index rebuilt'}


@fix('pitr_client_image', health.TIER_CONFIRM, 'Review client image change',
     "Sets the job's binlog client image to the one matching the database and restarts the streamer.", order=88)
def _plan_client_image(issue):
    job = _job_of(issue)
    suggested = issue.evidence.get('suggested_image')
    if not suggested:
        raise NotApplicable('no suggested image')
    current = (job.get('pitr_client_image') or '').strip()
    if current == suggested:
        raise NotApplicable('the job already uses that image')
    return _plan('pitr_client_image', f"Use {suggested} as the binlog client of {job.get('name')}",
                 [f"Set the job's PITR client image to {suggested}",
                  'Restart the binlog streamer with it (docker pull on first use)'],
                 downtime='None for the database; shipping pauses for the restart (seconds)',
                 details={'job': job.get('name'), 'current_image': current or '(host binaries)',
                          'new_image': suggested, 'server_version': issue.evidence.get('server_version')},
                 warnings=['The server needs docker (the prerequisite check found it).'])


@executes('pitr_client_image')
def _exec_client_image(ctx, plan, issue):
    m = _m()
    job_id = issue.resource['job_id']
    job = m.get_job(job_id)
    job['pitr_client_image'] = plan['details']['new_image']
    m.save_job(job_id, job)
    ctx.log(f"Client image set to {plan['details']['new_image']}")
    message = _pitr_tick_forced(ctx, job_id)
    probe_prereqs(job_id, dict(job, id=job_id))
    return {'message': message}


# Host keys ----------------------------------------------------------------------

def _host_pattern(host, port):
    port = int(port or 22)
    return host if port == 22 else f'[{host}]:{port}'


def _fingerprints(lines):
    """['ED25519 SHA256:...', ...] for known_hosts-format key lines."""
    if not lines:
        return []
    proc = subprocess.run(['ssh-keygen', '-l', '-E', 'sha256', '-f', '-'], input='\n'.join(lines) + '\n',
                          capture_output=True, text=True, timeout=30)
    out = []
    for line in proc.stdout.splitlines():
        m = re.search(r'(SHA256:\S+).*\((\w[\w-]*)\)\s*$', line)
        if m:
            out.append(f'{m.group(2)} {m.group(1)}')
    return sorted(set(out))


def known_host_fingerprints(host, port):
    path = _m().known_hosts_path()
    if not os.path.exists(path):
        return []
    proc = subprocess.run(['ssh-keygen', '-F', _host_pattern(host, port), '-f', path],
                          capture_output=True, text=True, timeout=30)
    lines = [l for l in proc.stdout.splitlines() if l.strip() and not l.startswith('#')]
    return _fingerprints(lines)


def scan_host_keys(host, port):
    """(known_hosts lines, fingerprints) the server presents now."""
    proc = subprocess.run(['ssh-keyscan', '-T', '10', '-p', str(int(port or 22)), host],
                          capture_output=True, text=True, timeout=40)
    lines = [l for l in proc.stdout.splitlines() if l.strip() and not l.startswith('#')]
    return lines, _fingerprints(lines)


@fix('trust_host_key', health.TIER_CONFIRM, 'Review new host key',
     "Replaces the server's recorded SSH host key with the one it presents now.", order=4)
def _plan_trust(issue):
    server = _m().get_server(issue.resource.get('id'))
    if not server:
        raise NotApplicable('the server no longer exists')
    host, port = server.get('host'), int(server.get('ssh_port') or 22)
    old = known_host_fingerprints(host, port)
    _lines, new = scan_host_keys(host, port)
    if not new:
        raise NotApplicable("the server didn't present any host key (unreachable?)")
    if old and set(new) <= set(old):
        raise NotApplicable('the presented key is already trusted')
    return _plan('trust_host_key', f"Trust the new SSH host key of {server.get('name')}",
                 [f'Remove the recorded key(s) of {_host_pattern(host, port)} from backupx\'s known_hosts',
                  'Record the key(s) the server presents now (fingerprints below)', 'Re-test SSH'],
                 details={'server': server.get('name'), 'host': _host_pattern(host, port),
                          'old_fingerprints': old or ['(none recorded)'], 'new_fingerprints': new},
                 warnings=['Compare the new fingerprint with the server itself (ssh-keygen -lf '
                           '/etc/ssh/ssh_host_ed25519_key.pub) before confirming.'])


@executes('trust_host_key')
def _exec_trust(ctx, plan, issue):
    m = _m()
    server = m.get_server(issue.resource.get('id'))
    host, port = server.get('host'), int(server.get('ssh_port') or 22)
    lines, new = scan_host_keys(host, port)
    if sorted(new) != sorted(plan['details']['new_fingerprints']):
        raise tasks.TaskError('The server now presents a different key than the one you confirmed; nothing changed')
    path = m.known_hosts_path()
    with _known_hosts_lock:
        if os.path.exists(path):
            proc = subprocess.run(['ssh-keygen', '-R', _host_pattern(host, port), '-f', path],
                                  capture_output=True, text=True, timeout=30)
            ctx.log((proc.stdout or proc.stderr).strip())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            for line in lines:
                f.write(line.strip() + '\n')
    ctx.log(f"Recorded {len(lines)} host key line(s): {', '.join(new)}")
    data = probe_server(server)
    if not data.get('ok'):
        raise tasks.TaskError(f"Key recorded, but SSH still fails: {data.get('error')}")
    return {'message': f"Trusted the new host key of {server.get('name')}", 'fingerprints': new}


_known_hosts_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Running fixes
# ---------------------------------------------------------------------------

def resource_lock_key(issue):
    r = issue.resource
    if r.get('job_id') and r.get('type') == 'storage':
        return f"fix:job:{r['job_id']}:{r.get('storage_id') or ''}"
    if r.get('job_id'):
        return f"fix:job:{r['job_id']}"
    return f"fix:{r.get('type')}:{r.get('id')}"


def prepare(issue_id, state=None):
    """(issue, fix) for an issue id, or raise NotApplicable / LookupError."""
    issue = find_issue(issue_id, state)
    if issue is None:
        raise LookupError('issue_resolved')
    f = FIXES.get(issue.fix) if issue.fix else None
    if f is None:
        raise NotApplicable('manual')
    return issue, f


def enqueue_fix(issue, f, plan_hash=None, created_by=None, ip=None, parent_id=None, client_key=None):
    params = {'issue_id': issue.id, 'action': f.action, 'resource': issue.resource, 'plan_hash': plan_hash,
              'client_key': client_key}
    title = f"{f.label}: {issue.resource.get('job_name') or issue.resource.get('name')}"
    if issue.resource.get('storage_name') and issue.resource.get('type') == 'storage':
        title += f" / {issue.resource['storage_name']}"
    rtype = 'job' if issue.resource.get('job_id') else issue.resource.get('type')
    rid = issue.resource.get('job_id') or issue.resource.get('id')
    return tasks.enqueue(f'fix.{f.action}', params, title=title, resource_type=rtype, resource_id=rid,
                         parent_id=parent_id, tier=f.tier, idempotency_key=f'fix:{issue.id}',
                         created_by=created_by, ip=ip)


def _audit(ctx, action, issue, status, error=None, details=None):
    try:
        _m()._audit(action, issue.resource.get('type') or 'job',
                    issue.resource.get('job_id') or issue.resource.get('id'),
                    issue.resource.get('job_name') or issue.resource.get('name'),
                    dict(details or {}, issue_id=issue.id, kind=issue.kind, task_id=ctx.id),
                    status=status, error=error, user=ctx.created_by or 'system', ip=ctx.ip)
    except Exception:
        pass


def _make_handler(action):
    def handler(ctx):
        from .security import plan_hash
        f = FIXES[action]
        issue_id = ctx.params.get('issue_id')
        ctx.step(1, 3, 'Re-checking the issue')
        issue = find_issue(issue_id)
        if issue is None:
            ctx.log('The issue is gone; nothing to do', stream='system')
            return {'message': 'Already resolved', 'skipped': True}
        if issue.fix != action:
            raise tasks.TaskError('The issue changed since the fix was requested; review it again')
        try:
            plan = f.plan(issue)
        except NotApplicable as e:
            raise tasks.TaskError(f'Not applicable anymore: {e}')
        expected = ctx.params.get('plan_hash')
        if expected and expected != plan_hash(plan):
            raise tasks.TaskError('State changed since you confirmed the plan; nothing done. Review it again.')
        if f.tier != health.TIER_SAFE and not expected:
            raise tasks.TaskError('A confirmed plan is required for this fix')
        for line in plan.get('steps', []):
            ctx.log(f'Plan: {line}', stream='system')
        ctx.step(2, 3, plan['title'])
        try:
            with ctx.lock(resource_lock_key(issue), wait=120, what='Another fix for this resource'):
                result = f.exec_fn(ctx, plan, issue) or {}
        except tasks.TaskCancelled:
            _audit(ctx, 'HEALTH_FIX_DONE', issue, 'cancelled', details={'action': action})
            raise
        except Exception as e:
            _audit(ctx, 'HEALTH_FIX_DONE', issue, 'failure', error=str(e)[:300], details={'action': action})
            raise
        _audit(ctx, 'HEALTH_FIX_DONE', issue, 'success', details={'action': action})
        try:
            sync_findings(current_issues())
        except Exception as e:
            logger.debug(f"findings sync after fix failed: {e}")
        return result
    return handler


NON_CANCELLABLE = {'run_now', 'rerun_failed', 'storage_retry', 'init_repo', 'install_restic', 'anchored_dump',
                   'unlock_stale', 'unlock_lock', 'remove_key', 'trust_host_key', 'pitr_kick', 'walg_repush',
                   'pitr_client_image', 'retry_forget', 'repair_index'}

for _action, _f in FIXES.items():
    tasks.register(f'fix.{_action}', title=_f.label,
                   tier=tasks.TIER_CONFIRM if _f.tier == health.TIER_CONFIRM else tasks.TIER_SAFE,
                   cancellable=_action not in NON_CANCELLABLE)(_make_handler(_action))


def safe_candidates(state=None, issue_ids=None):
    """Open, not snoozed issues whose fix is safe (what 'Fix all safe issues' would run)."""
    findings = load_findings()
    now = _now()
    out = []
    for issue in current_issues(state):
        f = FIXES.get(issue.fix) if issue.fix else None
        if not f or f.tier != health.TIER_SAFE:
            continue
        snoozed = health.utc((findings.get(issue.id) or {}).get('snoozed_until'))
        if snoozed and snoozed > now:
            continue
        if issue_ids is not None and issue.id not in issue_ids:
            continue
        out.append((issue, f))
    return out


@tasks.register('fix.all_safe', title='Fix all safe issues', tier=tasks.TIER_SAFE)
def _task_fix_all(ctx):
    ids = set(ctx.params.get('issue_ids') or [])
    done, failed, skipped = [], [], []
    waves = sorted({f.order for _i, f in safe_candidates(issue_ids=ids)})
    total = max(1, len(waves))
    for n, order in enumerate(waves, 1):
        ctx.check_cancelled()
        wave = [(i, f) for i, f in safe_candidates(issue_ids=ids) if f.order == order]
        ctx.step(n, total + 1, f'Fixing {len(wave)} issue(s): {", ".join(sorted({f.label for _i, f in wave}))}')
        children = []
        for issue, f in wave:
            row, _created = enqueue_fix(issue, f, created_by=ctx.created_by, ip=ctx.ip, parent_id=ctx.id)
            children.append((issue, row['id']))
            ctx.log(f'{f.label}: {issue.title} (task {row["id"]})')
        pending = {tid for _i, tid in children}
        while pending:
            if ctx.cancel_event.wait(2):
                for tid in pending:
                    tasks.request_cancel(tid)
                raise tasks.TaskCancelled()
            for tid in list(pending):
                row = tasks.get_task(tid)
                if row is None or row['status'] in tasks.FINAL_STATUSES:
                    pending.discard(tid)
                    issue = next(i for i, t in children if t == tid)
                    status = row['status'] if row else 'gone'
                    (done if status == tasks.SUCCEEDED else failed).append(issue.id)
                    ctx.log(f"{issue.title}: {status}{' - ' + row['error'] if row and row.get('error') else ''}")
        ids -= {i.id for i, _t in children}
    skipped = sorted(ids)
    result = {'fixed': len(done), 'failed': len(failed), 'skipped': len(skipped),
              'message': f'{len(done)} fixed, {len(failed)} failed' + (f', {len(skipped)} no longer applicable'
                                                                       if skipped else '')}
    if failed and not done:
        ctx.result = result
        raise tasks.TaskError(result['message'])
    return result


@tasks.register('health_sweep', title='Health check')
def _task_sweep(ctx):
    rtype, rid = ctx.params.get('resource_type'), ctx.params.get('resource_id')
    ctx.step(1, 2, 'Checking servers and repositories' if not rtype else f'Checking {rtype}')
    result = sweep(ctx, rtype, rid, force_expensive=bool(ctx.params.get('force')))
    result['message'] = (f"Checked {result['checked']} item(s): {result['issues']} issue(s), "
                         f"{result['critical']} critical")
    return result


# ---------------------------------------------------------------------------
# Dashboard overview
# ---------------------------------------------------------------------------

def dashboard_overview(job_id=None):
    """Job cards, activity and storage usage; job_id limits it to one job (job page)."""
    m = _m()
    state = collect_state()
    if job_id:
        state['jobs'] = {k: v for k, v in state['jobs'].items() if k == job_id}
    issues = health.detect(state)
    findings = load_findings()
    now = _now()
    counts = {}
    for i in issues:
        snoozed = health.utc((findings.get(i.id) or {}).get('snoozed_until'))
        if snoozed and snoozed > now:
            continue
        jid = i.resource.get('job_id')
        if jid:
            counts.setdefault(jid, {'critical': 0, 'warning': 0, 'info': 0})[i.severity] += 1
    try:
        from . import pitr_service
        pitr_states = pitr_service.summaries(state['jobs'])
    except Exception:
        pitr_states = {}
    next_runs = {}
    try:
        for sj in m.scheduler.get_jobs():
            if sj.next_run_time:
                next_runs[sj.id] = sj.next_run_time.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    tz_name = m.get_scheduler_timezone()
    cards = []
    for jid, job in state['jobs'].items():
        latest = state['latest_results'].get(jid) or {}
        storages, failed_ids, newest = [], [], None
        for st in state['job_storages'].get(jid) or []:
            r = latest.get(st['id'] or '')
            storages.append({'id': st['id'], 'name': st['name'], 'status': r.get('status') if r else None,
                             'finished_at': _iso(r.get('finished_at')) if r else None,
                             'error': (r.get('error') or None) if r else None})
            if r and r.get('status') != 'success' and st['id']:
                failed_ids.append(st['id'])
            if r and r.get('finished_at') and (newest is None or r['finished_at'] > newest['finished_at']):
                newest = r
        last_run_id = newest.get('run_id') if newest else None
        ret = state['retention'].get(jid) or []
        from . import retention as _ret
        forget = [r for r in ret if r.get('action') == 'forget']
        if not _ret.settings()['enabled'] or not _ret.retention_policy(job):
            rstate = {'state': 'off', 'last_at': None, 'message': None}
        elif not forget:
            rstate = {'state': 'never', 'last_at': None, 'message': None}
        else:
            worst = 'failed' if any(r['status'] == 'failed' for r in forget) else (
                'skipped' if any(r['status'] == 'skipped' for r in forget) else 'ok')
            newest = max(forget, key=lambda r: r.get('timestamp') or '')
            rstate = {'state': worst, 'last_at': newest.get('timestamp'), 'message': newest.get('message')}
        next_run = next_runs.get(jid)
        if not next_run and job.get('schedule_enabled'):
            try:
                trig = m.cron_trigger((job.get('schedule_cron') or '0 2 * * *').strip(), tz_name)
                nxt = trig.get_next_fire_time(None, now)
                next_run = nxt.astimezone(timezone.utc).isoformat() if nxt else None
            except Exception:
                next_run = None
        p = pitr_states.get(jid)
        server = state['servers'].get(job.get('server_id')) or {}
        cards.append({
            'id': jid, 'name': job.get('name'), 'backup_type': job.get('backup_type') or 'filesystem',
            'server_name': server.get('name'), 'status': job.get('status') or 'never',
            'running': job.get('status') == 'running', 'schedule_enabled': bool(job.get('schedule_enabled')),
            'schedule_cron': job.get('schedule_cron'), 'next_run': next_run,
            'last_run': _iso(job.get('last_run')), 'last_success': _iso(job.get('last_success')),
            'overdue_since': job.get('overdue_since'),
            'issues': counts.get(jid, {'critical': 0, 'warning': 0, 'info': 0}),
            'storages': storages, 'last_run_id': last_run_id, 'failed_storage_ids': failed_ids,
            'pitr': ({'enabled': p['enabled'], 'state': p['state'], 'window_start': p['window_start'],
                      'window_end': p['window_end'], 'lag_minutes': p['lag_minutes'],
                      'last_error': p.get('last_error')} if p else None),
            'retention': rstate,
            'last_verified_restore': _drill_card(state['probes'].get(f'drill:{jid}')),
        })
    cards.sort(key=lambda c: (-(c['issues']['critical'] * 100 + c['issues']['warning']), (c['name'] or '').lower()))
    usage, checked = [], None
    for key, probe in state['probes'].items():
        if not key.startswith('stats:'):
            continue
        _p, jid, sid = key.split(':', 2)
        data = probe.get('data') or {}
        if jid not in state['jobs']:
            continue
        usage.append({'storage_id': sid, 'storage_name': data.get('storage_name') or sid, 'job_id': jid,
                      'job_name': state['jobs'][jid].get('name'), 'bytes': data.get('bytes'),
                      'files': data.get('files'), 'snapshots': data.get('snapshots'),
                      'checked_at': _iso(probe.get('checked_at'))})
        if probe.get('checked_at') and (checked is None or probe['checked_at'] > checked):
            checked = probe['checked_at']
    events = activity(state)
    if job_id:
        events = [e for e in events if e.get('job_id') == job_id]
    return {'jobs': cards, 'activity': events, 'storage_usage': usage,
            'storage_usage_checked_at': _iso(checked)}


def _drill_card(probe):
    from . import drill_service
    return drill_service.serialize((probe or {}).get('data')) if probe else None


def activity(state=None, days=7, limit=100):
    db = _db()
    since = tasks.utcnow() - timedelta(days=days)
    events = []
    for r in db.fetchall('SELECT timestamp, job_id, job_name, status, message FROM history WHERE timestamp >= %s '
                         'ORDER BY timestamp DESC LIMIT 100', (since,)):
        status = 'success' if r['status'] == 'success' else 'failed'
        events.append({'ts': _iso(r['timestamp']), 'type': 'backup', 'status': status,
                       'title': f"{r['job_name']}: backup {'succeeded' if status == 'success' else r['status']}",
                       'detail': (r.get('message') or '')[:300], 'job_id': r['job_id']})
    for r in db.fetchall('SELECT * FROM retention_runs WHERE timestamp >= %s ORDER BY timestamp DESC LIMIT 50',
                         (since,)):
        name = ((state or {}).get('jobs', {}).get(r['job_id']) or {}).get('name') or r['job_id']
        status = {'success': 'success', 'failed': 'failed', 'skipped': 'warning'}.get(r['status'], 'info')
        events.append({'ts': _iso(r['timestamp']), 'type': 'retention', 'status': status,
                       'title': f"{name}: {r['action']} on {r.get('storage_name') or '?'} {r['status']}",
                       'detail': (r.get('message') or '')[:300], 'job_id': r['job_id']})
    for r in db.fetchall("SELECT * FROM tasks WHERE created_at >= %s AND kind NOT IN ('backup_run') "
                         "AND NOT (kind = 'health_sweep' AND created_by IS NULL) "
                         "ORDER BY created_at DESC LIMIT 50", (since,)):
        status = {'succeeded': 'success', 'failed': 'failed', 'running': 'running', 'queued': 'running',
                  'cancelled': 'warning', 'rolled_back': 'warning'}.get(r['status'], 'info')
        events.append({'ts': _iso(r.get('finished_at') or r.get('started_at') or r['created_at']), 'type': 'task',
                       'status': status, 'title': r.get('title') or r['kind'],
                       'detail': r.get('error') or r.get('step'), 'task_id': r['id'],
                       'job_id': r.get('resource_id') if r.get('resource_type') == 'job' else None})
    for r in db.fetchall('SELECT job_id, state, state_since FROM pitr_state WHERE state_since >= %s', (since,)):
        name = ((state or {}).get('jobs', {}).get(r['job_id']) or {}).get('name') or r['job_id']
        status = 'success' if r['state'] == 'ok' else ('failed' if r['state'] in ('broken', 'lagging') else 'info')
        events.append({'ts': _iso(r['state_since']), 'type': 'pitr', 'status': status,
                       'title': f"{name}: point-in-time recovery {r['state']}", 'detail': None, 'job_id': r['job_id']})
    for r in db.fetchall("SELECT timestamp, user_name, action, resource_type, resource_name, status FROM audit_log "
                         "WHERE timestamp >= %s AND action NOT IN ('LOGIN', 'LOGOUT', 'VIEW', 'READ') "
                         "ORDER BY timestamp DESC LIMIT 40", (since,)):
        events.append({'ts': _iso(r['timestamp']), 'type': 'audit',
                       'status': 'success' if r.get('status') == 'success' else 'failed',
                       'title': f"{r.get('user_name') or 'system'}: {r['action'].replace('_', ' ').lower()} "
                                f"{r.get('resource_name') or r.get('resource_type') or ''}".strip(),
                       'detail': None})
    events.sort(key=lambda e: e['ts'] or '', reverse=True)
    return events[:limit]

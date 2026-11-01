"""
Health checks: pure decisions (no database, no SSH, no restic).

app.health_service gathers a *state snapshot* (database rows plus the cached
results of the expensive probes) and hands it to `detect()` here, which turns
it into a list of Issues. Each Issue names the fix that applies (an action in
health_service.FIXES) and its tier:

- safe: one click, and part of "Fix all safe issues";
- confirm: the user reviews an exact plan, types a phrase and re-enters the
  password (step-up);
- manual: backupx can't fix it; the issue carries steps and commands.

Issue ids are stable hashes of the issue *family* and the resource, so the
same problem keeps its id across checks (first_seen, snooze and the Telegram
de-duplication hang off it) even when its details or variant change.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

SEV_CRITICAL = 'critical'
SEV_WARNING = 'warning'
SEV_INFO = 'info'
SEVERITIES = (SEV_CRITICAL, SEV_WARNING, SEV_INFO)
SEVERITY_RANK = {SEV_CRITICAL: 0, SEV_WARNING: 1, SEV_INFO: 2}

TIER_SAFE = 'safe'
TIER_CONFIRM = 'confirm'
TIER_MANUAL = 'manual'

# A restic lock not refreshed for this long belongs to a dead (or hung)
# process: restic refreshes its locks every 5 minutes and itself treats locks
# older than 30 minutes as stale.
STALE_LOCK_MIN_AGE = 30 * 60

LOW_DISK_PCT = 90


def issue_id(family: str, *parts) -> str:
    raw = '\x1f'.join([family] + [str(p or '') for p in parts])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Issue:
    family: str                 # stable part of the id (e.g. 'backup_failed')
    kind: str                   # what the UI shows/filters on (may be a variant of the family)
    severity: str
    title: str
    explanation: str
    resource: Dict[str, Any]    # {type, id, name, job_id?, storage_id?, server_id?, ...}
    key: tuple = ()             # resource parts of the id
    evidence: Dict[str, Any] = field(default_factory=dict)
    evidence_text: Optional[str] = None
    fix: Optional[str] = None   # action name in health_service.FIXES
    manual: Optional[Dict[str, List[str]]] = None
    alert: bool = True          # False: already notified elsewhere (backup failure mails, PITR transitions)

    @property
    def id(self) -> str:
        return issue_id(self.family, *self.key)

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'family': self.family, 'kind': self.kind, 'severity': self.severity,
            'title': self.title, 'explanation': self.explanation, 'resource': dict(self.resource),
            'evidence': dict(self.evidence), 'evidence_text': self.evidence_text,
            'fix_action': self.fix, 'manual': self.manual, 'alert': self.alert,
        }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def utc(value) -> Optional[datetime]:
    """Datetime (naive = UTC), epoch or ISO string -> aware UTC; None if unusable."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace('Z', '+00:00')
        # restic prints nanoseconds; fromisoformat takes at most microseconds
        text = re.sub(r'(\.\d{6})\d+', r'\1', text)
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def age_seconds(value, now) -> Optional[float]:
    t = utc(value)
    return (now - t).total_seconds() if t else None


def human_age(seconds) -> str:
    if seconds is None:
        return 'unknown'
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f'{seconds} s'
    if seconds < 90 * 60:
        return f'{seconds // 60} min'
    if seconds < 48 * 3600:
        return f'{seconds // 3600} h {seconds % 3600 // 60} min'
    return f'{seconds // 86400} days'


def _short(text, limit=300):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


# ---------------------------------------------------------------------------
# Error classification (restic / S3 / SSH output)
# ---------------------------------------------------------------------------

_WRONG_PASSWORD = re.compile(r'wrong password|no key found|ciphertext verification failed', re.I)
_LOCKED = re.compile(r'repository is already locked|unable to create lock|already locked', re.I)
_NOT_INIT = re.compile(r'Is there a repository at the following location', re.I)
_RESTIC_MISSING = re.compile(r'restic: (command )?not found|Restic not available|restic: No such file', re.I)
_UNREACHABLE = re.compile(
    r'\b(50[0-4]|52[0-9]|429)\b|connection refused|connection reset|timed? ?out|timeout|no such host|'
    r'dial tcp|i/o timeout|server misbehaving|service unavailable|bad gateway|gateway time|'
    r'network is unreachable|tls handshake|unexpected EOF|\bEOF\b|could not resolve|temporary failure',
    re.I)
_HOST_KEY_CHANGED = re.compile(r'REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed|'
                               r'host key for .* has changed', re.I)


def classify_error(text: str) -> str:
    """'wrong_password' | 'locked' | 'not_initialized' | 'restic_missing' | 'host_key' | 'unreachable' | 'other'."""
    text = text or ''
    if _WRONG_PASSWORD.search(text):
        return 'wrong_password'
    if _LOCKED.search(text):
        return 'locked'
    if _HOST_KEY_CHANGED.search(text):
        return 'host_key'
    if _RESTIC_MISSING.search(text):
        return 'restic_missing'
    if _NOT_INIT.search(text) and re.search(r'does not exist|no such file', text, re.I):
        return 'not_initialized'
    if _UNREACHABLE.search(text):
        return 'unreachable'
    return 'other'


# ---------------------------------------------------------------------------
# Stale restic locks
# ---------------------------------------------------------------------------

def _host_names(value) -> set:
    names = set()
    for v in value or ():
        v = (v or '').strip().lower()
        if v:
            names.add(v)
            names.add(v.split('.')[0])
    return names


def lock_decision(lock: dict, now: datetime, managed_hosts, backupx_hosts, advisory_held: bool,
                  repo_in_use: bool, min_age: int = STALE_LOCK_MIN_AGE):
    """How a restic lock may be removed: ('auto' | 'confirm' | 'wait', reason).

    auto    - older than min_age, made by a backupx-managed server or the backupx
              container, no backupx advisory lock (job run, PITR tick, prune)
              and repo_in_use() false: the "Fix all" button may unlock it.
    confirm - stale, nothing of backupx's uses the repository, but the lock
              comes from a host backupx doesn't manage: the admin decides.
    wait    - fresh (a restic is probably still working) or backupx is using
              the repository right now: never unlock.
    """
    age = age_seconds(lock.get('time'), now)
    if age is None:
        return 'wait', "the lock's time can't be read"
    if age < min_age:
        return 'wait', f'the lock is {human_age(age)} old; restic refreshes live locks every 5 min'
    if advisory_held:
        return 'wait', 'a backupx run (backup, PITR shipping or prune) is using this repository'
    if repo_in_use:
        return 'wait', 'backupx is using this repository right now'
    host = (lock.get('hostname') or lock.get('host') or '').strip().lower()
    if host and (host in _host_names(managed_hosts) or host.split('.')[0] in _host_names(managed_hosts)):
        return 'auto', f'made on {host} (a server backupx manages), not refreshed for {human_age(age)}'
    if host and (host in _host_names(backupx_hosts) or host.split('.')[0] in _host_names(backupx_hosts)):
        return 'auto', f'made by the backupx container ({host}), not refreshed for {human_age(age)}'
    return 'confirm', f'made on {host or "an unknown host"}, which backupx does not manage'


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _job_resource(job_id, job, storage=None):
    res = {'type': 'job', 'id': job_id, 'name': job.get('name') or job_id, 'job_id': job_id,
           'job_name': job.get('name') or job_id}
    if job.get('server_id'):
        res['server_id'] = job['server_id']
    if storage:
        res.update(type='storage', id=storage.get('id') or '', name=storage.get('name') or '?',
                   storage_id=storage.get('id') or '', storage_name=storage.get('name') or '?')
    return res


def detect_overdue(state) -> List[Issue]:
    out = []
    for job_id, job in state['jobs'].items():
        since = job.get('overdue_since')
        if not since:
            continue
        out.append(Issue(
            'job_overdue', 'job_overdue', SEV_CRITICAL,
            f"{job.get('name') or job_id} missed its scheduled backup",
            "The job should have started a backup by its schedule but hasn't. The scheduler may have been down "
            "or the run queue full. Running it now takes a fresh backup.",
            _job_resource(job_id, job), key=(job_id,),
            evidence={'due_at': since if isinstance(since, str) else utc(since).isoformat(),
                      'last_run': job.get('last_run'), 'schedule': job.get('schedule_cron')},
            fix='run_now'))
    return out


def detect_backup_failures(state) -> List[Issue]:
    """Latest result per active storage; a job with any failed storage gets one issue."""
    out = []
    for job_id, job in state['jobs'].items():
        if job.get('status') == 'running':
            continue
        storages = state['job_storages'].get(job_id) or []
        latest = state['latest_results'].get(job_id) or {}
        failed, ok, wrong_pw, locked, classes = [], [], [], [], set()
        for st in storages:
            r = latest.get(st.get('id') or '')
            if r is None:
                continue
            if r.get('status') == 'success':
                ok.append(st)
                continue
            cls = classify_error(r.get('error') or '')
            if cls == 'wrong_password':
                wrong_pw.append((st, r))
                continue
            if cls == 'restic_missing':
                classes.add(cls)
                continue  # the server's restic_missing issue covers it
            if cls == 'locked':
                locked.append(st)
            classes.add(cls)
            failed.append((st, r))
        if not latest and job.get('status') in ('failed', 'error') and storages:
            # Failed before per-storage results existed (or before reaching any storage)
            failed = [(st, {'status': 'failed', 'error': job.get('progress_message') or 'backup failed'})
                      for st in storages]
            classes.add(classify_error(job.get('progress_message') or ''))
        for st, r in wrong_pw:
            out.append(Issue(
                'repo_open_error', 'repo_open_error', SEV_CRITICAL,
                f"Can't open the repository on {st.get('name')}",
                "restic says the password is wrong (no key in the repository matches the job's restic "
                "password). backupx can't fix this: the job's password or the repository was changed.",
                _job_resource(job_id, job, st), key=(job_id, st.get('id') or ''),
                evidence={'storage': st.get('name'), 'error': _short(r.get('error'))},
                manual={'steps': [
                    "Check whether the job's restic password was changed (Jobs > Edit > reveal password).",
                    "If the repository was re-created elsewhere, point the job at it with the right password, "
                    "or move the old repository away and let the next backup create a new one.",
                    "Then press Re-check."],
                    'commands': [f"export RESTIC_REPOSITORY='{st.get('repo_url', '<repository URL>')}'",
                                 'restic cat config   # fails with "wrong password" until it matches',
                                 'restic key list']},
                alert=False))
        if not failed:
            continue
        names = [st.get('name') or '?' for st, _ in failed]
        all_failed = not ok
        unreachable = classes == {'unreachable'}
        what = (f"backup failed on {len(failed)} of {len(storages)} storages" if len(storages) > 1
                else 'last backup failed')
        title = f"{job.get('name') or job_id}: {what}"
        if unreachable:
            explanation = ("The storage could not be reached (timeouts, connection errors or HTTP 5xx/524). "
                           "These are usually temporary: backupx re-tests the storage and retries the failed "
                           "storages with a back-off.")
        elif locked:
            explanation = ("restic found the repository locked. If the lock is left over from a crashed run, "
                           "the stale-lock fix removes it; then the failed storages are retried.")
        else:
            explanation = ("The last run did not store a snapshot on " + ', '.join(names) +
                           ". Retrying runs the backup again on those storages only.")
        evidence = {'failed_storages': ', '.join(names), 'last_attempt': None}
        text = []
        for st, r in failed:
            if r.get('finished_at'):
                evidence['last_attempt'] = r['finished_at'] if isinstance(r['finished_at'], str) \
                    else utc(r['finished_at']).isoformat()
            if r.get('run_id'):
                evidence['run_id'] = r['run_id']
            text.append(f"{st.get('name')}: {_short(r.get('error'), 500)}")
        out.append(Issue(
            'backup_failed', 'storage_unreachable' if unreachable else 'backup_failed',
            SEV_CRITICAL if all_failed else SEV_WARNING, title, explanation,
            _job_resource(job_id, job), key=(job_id,), evidence=evidence, evidence_text='\n'.join(text),
            fix='storage_retry' if unreachable else 'rerun_failed', alert=False))
    return out


def detect_retention(state) -> List[Issue]:
    out = []
    for job_id, runs in (state.get('retention') or {}).items():
        job = state['jobs'].get(job_id)
        if not job:
            continue
        for r in runs:
            if r.get('status') != 'failed':
                continue
            st = {'id': r.get('storage_id') or '', 'name': r.get('storage_name') or '?'}
            msg = r.get('message') or ''
            if r.get('action') == 'forget' and 'refused' in msg:
                out.append(Issue(
                    'retention_failed', 'retention_refused', SEV_WARNING,
                    f"{job.get('name')}: retention refused on {st['name']}",
                    "restic's forget plan would have removed every snapshot of a group, so backupx refused "
                    "and removed nothing. This usually means the keep rules don't match how the job is "
                    "tagged. Review the job's keep rules.",
                    _job_resource(job_id, job, st), key=(job_id, st['id'], r.get('action')),
                    evidence={'storage': st['name'], 'at': _iso(r.get('timestamp')), 'message': _short(msg)},
                    manual={'steps': ["Open the job and check its keep-hourly/daily/weekly/monthly values.",
                                      "Compare with the snapshots listed for the storage (Snapshots tab).",
                                      "Re-check after the next backup."], 'commands': []},
                    alert=False))
            elif r.get('action') == 'forget':
                out.append(Issue(
                    'retention_failed', 'retention_failed', SEV_WARNING,
                    f"{job.get('name')}: retention (forget) failed on {st['name']}",
                    "Old snapshots were not removed after the last backup. If a stale lock blocked it, "
                    "the fix removes the lock (only if safe) and runs forget again.",
                    _job_resource(job_id, job, st), key=(job_id, st['id'], 'forget'),
                    evidence={'storage': st['name'], 'at': _iso(r.get('timestamp')), 'message': _short(msg)},
                    fix='retry_forget', alert=False))
            else:
                out.append(Issue(
                    'retention_failed', 'prune_failed', SEV_WARNING,
                    f"{job.get('name')}: weekly prune failed on {st['name']}",
                    "Space from forgotten snapshots was not reclaimed. It runs again with the next weekly "
                    "prune; nothing is lost meanwhile.",
                    _job_resource(job_id, job, st), key=(job_id, st['id'], 'prune'),
                    evidence={'storage': st['name'], 'at': _iso(r.get('timestamp')), 'message': _short(msg)},
                    manual={'steps': ["Check the message; a stale lock shows up as its own issue.",
                                      "The next weekly prune retries automatically."], 'commands': []},
                    alert=False))
    return out


def _iso(value):
    t = utc(value)
    return t.isoformat() if t else None


def detect_pitr(state) -> List[Issue]:
    out = []
    now = state['now']
    max_age = state.get('pitr_heartbeat_max_age', 300)
    for job_id, job in state['jobs'].items():
        if not job.get('pitr_enabled'):
            continue
        ps = (state.get('pitr') or {}).get(job_id) or {}
        kind = ps.get('kind') or state.get('job_db_kinds', {}).get(job_id) or 'mysql'
        res = _job_resource(job_id, job)
        st = ps.get('state')
        info = ps.get('info') or {}
        evidence = {'state': st, 'window_end': _iso(ps.get('window_end')), 'lag_minutes': ps.get('lag_minutes'),
                    'last_error': _short(ps.get('last_error')) if ps.get('last_error') else None}
        if kind == 'mysql':
            hb_age = age_seconds(ps.get('heartbeat'), now)
            active = ps.get('streamer_active')
            down = ps.get('last_tick_at') and (
                (active and active != 'active') or (hb_age is not None and hb_age > max_age))
            failing = int(ps.get('consecutive_failures') or 0) >= 3
            if down or failing:
                evidence.update(streamer=active, heartbeat_age=human_age(hb_age),
                                consecutive_failures=ps.get('consecutive_failures'))
                out.append(Issue(
                    'pitr_streamer', 'pitr_streamer_down',
                    SEV_CRITICAL if st in ('lagging', 'broken') else SEV_WARNING,
                    f"{job.get('name')}: binlog streamer {'stopped' if down else 'checks failing'}",
                    "The binlog streamer on the database server isn't running or has stalled, so new changes "
                    "aren't being captured for point-in-time recovery. The fix re-installs the streamer unit "
                    "and restarts it, then ships what's queued.",
                    res, key=(job_id,), evidence=evidence, fix='pitr_kick', alert=False))
        else:
            if ps.get('streamer_active') == 'failing':
                out.append(Issue(
                    'pitr_streamer', 'wal_archive_failing',
                    SEV_CRITICAL if st in ('lagging', 'broken') else SEV_WARNING,
                    f"{job.get('name')}: WAL archiving is failing",
                    "PostgreSQL's archive_command (wal-g wal-push) is failing, so WAL piles up on the server "
                    "and recent changes aren't restorable. The fix re-installs the wal-g environment and "
                    "archive script, then tests the storage.",
                    res, key=(job_id,), evidence=dict(evidence, archiver=info.get('archiver')),
                    fix='walg_repush', alert=False))
            elif ps.get('last_tick_at') and ps.get('streamer_active') == 'inactive':
                out.append(Issue(
                    'pitr_streamer', 'wal_archiving_off', SEV_WARNING,
                    f"{job.get('name')}: WAL archiving is not switched on yet",
                    "backupx installed wal-g, but postgresql.conf doesn't archive with it yet. backupx never "
                    "restarts PostgreSQL itself: apply the settings below and restart in a quiet moment.",
                    res, key=(job_id,), evidence=evidence,
                    manual={'steps': ["Add these settings to postgresql.conf (or ALTER SYSTEM SET ...).",
                                      "Restart PostgreSQL (archive_mode needs a restart).",
                                      "Press Re-check."],
                            'commands': list(state.get('pg_conf_changes') or [])}))
            mirror = [m for m in (info.get('mirror') or []) if not m.get('ok')]
            if mirror:
                out.append(Issue(
                    'wal_mirror', 'wal_mirror_failed', SEV_WARNING,
                    f"{job.get('name')}: WAL copy to other storages failed",
                    "WAL reaches the primary storage, but copying it to the other storages failed. The fix "
                    "retries the copy now.",
                    res, key=(job_id,),
                    evidence=dict(evidence, failed=', '.join(f"{m.get('storage')}: {m.get('error') or 'failed'}"
                                                             for m in mirror)[:300]),
                    fix='wal_mirror_retry'))
        if st in ('broken', 'unanchored'):
            prereq = (state.get('probes') or {}).get(f'prereq:{job_id}') or {}
            prereq_ok = (prereq.get('data') or {}).get('ok')
            broken = st == 'broken'
            title = (f"{job.get('name')}: point-in-time chain broken" if broken
                     else f"{job.get('name')}: point-in-time recovery has no anchor")
            explanation = ("Times after the break can't be restored until a new full backup anchors the chain. "
                           if broken else
                           "Binlogs/WAL are being captured, but no full backup marks where to start replaying. ")
            fix = 'anchored_dump' if prereq_ok is not False else None
            manual = None
            if fix:
                explanation += "Taking an anchored full backup now closes the gap (extra load on the database, " \
                               "no downtime)."
            else:
                explanation += "The prerequisite check fails, so an anchored dump wouldn't help yet."
                manual = {'steps': ["Open the job's PITR settings and run Check prerequisites.",
                                    "Fix the failing checks, then press Re-check."], 'commands': []}
            out.append(Issue(
                'pitr_unanchored', 'pitr_broken' if broken else 'pitr_unanchored',
                SEV_CRITICAL if broken else SEV_WARNING, title, explanation, res, key=(job_id,),
                evidence=evidence, fix=fix, manual=manual, alert=False))
        mismatch = ((state.get('probes') or {}).get(f'prereq:{job_id}') or {}).get('data', {}).get('image_mismatch')
        if mismatch and mismatch.get('suggested'):
            out.append(Issue(
                'pitr_client_image', 'pitr_client_image', SEV_WARNING,
                f"{job.get('name')}: binlog client doesn't match the database",
                "The mysqlbinlog/mysqldump used for point-in-time recovery doesn't match the database server "
                f"({mismatch.get('reason') or 'version/flavour mismatch'}). The fix sets the job's client image "
                f"to {mismatch['suggested']} and restarts the streamer with it.",
                res, key=(job_id,),
                evidence={'current_image': mismatch.get('current') or '(host binaries)',
                          'suggested_image': mismatch['suggested'], 'server_version': mismatch.get('server_version')},
                fix='pitr_client_image'))
    return out


def detect_scheduler(state) -> List[Issue]:
    sch = state.get('scheduler') or {}
    hb = sch.get('heartbeat_at')
    limit = sch.get('max_age', 120)
    age = age_seconds(hb, state['now'])
    if age is not None and age <= limit:
        return []
    return [Issue(
        'scheduler_down', 'scheduler_down', SEV_CRITICAL,
        'No scheduler is running',
        "No backupx worker holds the scheduler lock, so scheduled backups, PITR shipping and retention "
        "don't run. This needs a look at the container: it may be restarting, or every instance has "
        "SCHEDULER_ENABLED=false.",
        {'type': 'system', 'id': 'scheduler', 'name': 'Scheduler'}, key=('scheduler',),
        evidence={'last_heartbeat': _iso(hb), 'age': human_age(age) if age is not None else 'never'},
        manual={'steps': ["Check that the backupx container is running and healthy.",
                          "Look at its logs for scheduler/lock errors.",
                          "Make sure at least one instance runs with SCHEDULER_ENABLED=true (the default).",
                          "Press Re-check."],
                'commands': ['docker compose ps', 'docker compose logs --tail 200 backup-ui | grep -i scheduler',
                             'docker compose restart backup-ui']})]


def detect_channels(state) -> List[Issue]:
    out = []
    for ch in state.get('channels') or []:
        if not ch.get('enabled'):
            continue
        err_at, ok_at = utc(ch.get('last_error_at')), utc(ch.get('last_success_at'))
        if not err_at or (ok_at and ok_at >= err_at):
            continue
        out.append(Issue(
            'notification_failing', 'notification_failing', SEV_WARNING,
            f"Notifications to {ch.get('name')} are failing",
            "The last message to this channel could not be delivered, so alerts may be missed. The fix "
            "sends a test message; if it fails, check the channel's settings.",
            {'type': 'channel', 'id': ch['id'], 'name': ch.get('name') or ch['id']}, key=(ch['id'],),
            evidence={'type': ch.get('type'), 'last_error_at': _iso(err_at), 'last_success_at': _iso(ok_at),
                      'error': _short(ch.get('last_error'))},
            fix='notification_test', alert=False))
    return out


def detect_servers(state) -> List[Issue]:
    out = []
    used = state.get('servers_in_use') or set()
    for server_id, server in (state.get('servers') or {}).items():
        probe = (state.get('probes') or {}).get(f'ssh:{server_id}')
        if not probe:
            continue
        data = probe.get('data') or {}
        res = {'type': 'server', 'id': server_id, 'name': server.get('name') or server.get('host'),
               'server_id': server_id}
        checked = _iso(probe.get('checked_at'))
        if not data.get('ok'):
            if data.get('host_key_changed'):
                out.append(Issue(
                    'ssh', 'host_key_changed', SEV_CRITICAL,
                    f"{res['name']}: SSH host key changed",
                    "The server presented a different SSH host key than the one backupx recorded, so backupx "
                    "refuses to connect. That is expected after a reinstall or a new host key; otherwise it "
                    "could be an impersonation. Compare the fingerprints below with the server's before "
                    "trusting the new key.",
                    res, key=(server_id,),
                    evidence={'host': f"{server.get('host')}:{server.get('ssh_port') or 22}", 'checked_at': checked,
                              'error': _short(data.get('error'))},
                    fix='trust_host_key'))
            else:
                out.append(Issue(
                    'ssh', 'ssh_unreachable', SEV_CRITICAL if server_id in used else SEV_WARNING,
                    f"{res['name']}: can't connect over SSH",
                    "backupx couldn't log in to the server, so its backups (and PITR) can't run. The server "
                    "may be down, the network or firewall may block port "
                    f"{server.get('ssh_port') or 22}, or the credentials changed.",
                    res, key=(server_id,),
                    evidence={'host': f"{server.get('host')}:{server.get('ssh_port') or 22}", 'checked_at': checked,
                              'error': _short(data.get('error'))},
                    fix='ssh_retest'))
            continue
        if data.get('restic_missing') and data.get('restic_install_error'):
            out.append(Issue(
                'restic_missing', 'restic_missing', SEV_WARNING,
                f"{res['name']}: restic is not installed",
                "Backups install restic automatically, but it isn't on this server now. The fix installs the "
                "pinned static restic binary over SSH.",
                res, key=(server_id,), evidence={'checked_at': checked, 'error': _short(data.get('restic_install_error'))},
                fix='install_restic'))
        elif data.get('restic_missing'):
            out.append(Issue(
                'restic_missing', 'restic_missing', SEV_WARNING if server_id in used else SEV_INFO,
                f"{res['name']}: restic is not installed",
                "restic isn't on this server. The next backup installs it, or install it now.",
                res, key=(server_id,), evidence={'checked_at': checked}, fix='install_restic'))
        for disk in data.get('disks') or []:
            try:
                pct = int(str(disk.get('use_pct', '0')).rstrip('%'))
            except ValueError:
                continue
            if pct >= LOW_DISK_PCT:
                out.append(Issue(
                    'low_disk', 'low_disk', SEV_INFO,
                    f"{res['name']}: {disk.get('mount')} is {pct}% full",
                    "Database dumps, restic's cache and the PITR spool need free space on the server. "
                    "backupx doesn't delete anything there; free some space.",
                    res, key=(server_id, disk.get('mount')),
                    evidence={'mount': disk.get('mount'), 'used': f'{pct}%', 'available': disk.get('avail'),
                              'checked_at': checked},
                    manual={'steps': ["Find what uses the space and clean up.", "Press Re-check."],
                            'commands': [f"df -h {disk.get('mount')}",
                                         f"sudo du -xh --max-depth=2 {disk.get('mount')} | sort -h | tail -20",
                                         'du -sh ~/.cache/restic']}))
    return out


def detect_repos(state) -> List[Issue]:
    out = []
    now = state['now']
    for job_id, job in state['jobs'].items():
        for st in state['job_storages'].get(job_id) or []:
            sid = st.get('id') or ''
            res = _job_resource(job_id, job, st)
            probe = (state.get('probes') or {}).get(f'repo:{job_id}:{sid}')
            if probe:
                data = probe.get('data') or {}
                checked = _iso(probe.get('checked_at'))
                open_state = data.get('open')
                if open_state == 'not_initialized':
                    out.append(Issue(
                        'repo_not_initialized', 'repo_not_initialized', SEV_WARNING,
                        f"{job.get('name')}: no repository on {st.get('name')} yet",
                        "restic reports that the repository doesn't exist on this storage (exit code 10). The "
                        "next backup creates it; the fix creates it now.",
                        res, key=(job_id, sid), evidence={'storage': st.get('name'), 'checked_at': checked},
                        fix='init_repo'))
                elif open_state == 'wrong_password':
                    out.append(Issue(
                        'repo_open_error', 'repo_open_error', SEV_CRITICAL,
                        f"Can't open the repository on {st.get('name')}",
                        "restic says the password is wrong (no key in the repository matches the job's restic "
                        "password). backupx can't fix this.",
                        res, key=(job_id, sid),
                        evidence={'storage': st.get('name'), 'checked_at': checked,
                                  'error': _short(data.get('open_error'))},
                        manual={'steps': ["Check whether the job's restic password was changed.",
                                          "Point the job at the right repository/password, then Re-check."],
                                'commands': ['restic cat config', 'restic key list']}))
                elif open_state == 'error':
                    out.append(Issue(
                        'repo_unreachable', 'repo_unreachable', SEV_WARNING,
                        f"{job.get('name')}: can't reach the repository on {st.get('name')}",
                        "The health check couldn't open the repository (network, credentials or the storage "
                        "is down). Re-check tests it again.",
                        res, key=(job_id, sid),
                        evidence={'storage': st.get('name'), 'checked_at': checked,
                                  'error': _short(data.get('open_error'))},
                        fix='recheck_repo'))
                for lock in data.get('locks') or []:
                    decision = lock.get('decision') or 'wait'
                    age = age_seconds(lock.get('time'), now)
                    if age is None or age < STALE_LOCK_MIN_AGE or decision == 'wait':
                        continue
                    ev = {'storage': st.get('name'), 'lock_id': (lock.get('id') or '')[:12],
                          'host': lock.get('hostname'), 'pid': lock.get('pid'), 'user': lock.get('username'),
                          'created': _iso(lock.get('time')), 'age': human_age(age),
                          'exclusive': bool(lock.get('exclusive')), 'why': lock.get('reason')}
                    if decision == 'auto':
                        out.append(Issue(
                            'stale_lock', 'stale_lock', SEV_WARNING,
                            f"{job.get('name')}: stale restic lock on {st.get('name')}",
                            "A restic process that died (crash, reboot, killed run) left its lock behind. "
                            "Locks block backups and retention. It's safe to remove: it's older than 30 minutes, "
                            "comes from a host backupx manages and nothing of backupx is using the repository.",
                            res, key=(job_id, sid, lock.get('id')), evidence=ev, fix='unlock_stale'))
                    else:
                        out.append(Issue(
                            'stale_lock', 'stale_lock_foreign', SEV_WARNING,
                            f"{job.get('name')}: old restic lock from {lock.get('hostname') or 'another host'}",
                            "This lock hasn't been refreshed for over 30 minutes, but it comes from a host "
                            "backupx doesn't manage. Make sure no restic is running there before removing it.",
                            res, key=(job_id, sid, lock.get('id')), evidence=ev, fix='unlock_lock'))
                for key in data.get('keys') or []:
                    if key.get('current'):
                        continue
                    out.append(Issue(
                        'stray_key', 'stray_key', SEV_WARNING,
                        f"{job.get('name')}: extra restic key on {st.get('name')}",
                        "The repository has a key (password) besides the job's. Anyone with that password can "
                        "read the backups. Remove it unless you added it on purpose.",
                        res, key=(job_id, sid, key.get('id')),
                        evidence={'storage': st.get('name'), 'key_id': (key.get('id') or '')[:12],
                                  'user': key.get('userName'), 'host': key.get('hostName'),
                                  'created': key.get('created')},
                        fix='remove_key'))
            check = (state.get('probes') or {}).get(f'check:{job_id}:{sid}')
            if check and (check.get('data') or {}).get('ok') is False:
                data = check['data']
                out.append(Issue(
                    'repo_check_failed', 'repo_check_failed', SEV_CRITICAL,
                    f"{job.get('name')}: repository check failed on {st.get('name')}",
                    "restic check (structure only, no data read) found problems in the repository. Rebuilding "
                    "the index usually fixes index errors; backups of this job wait while it runs.",
                    res, key=(job_id, sid),
                    evidence={'storage': st.get('name'), 'checked_at': _iso(check.get('checked_at')),
                              'error': _short(data.get('error'))},
                    evidence_text=data.get('output_tail'), fix='repair_index'))
    return out


DETECTORS = (detect_scheduler, detect_overdue, detect_backup_failures, detect_retention, detect_pitr,
             detect_channels, detect_servers, detect_repos)


def detect(state) -> List[Issue]:
    """All issues for a state snapshot, critical first; one per id."""
    issues, seen = [], set()
    for detector in DETECTORS:
        for issue in detector(state):
            if issue.id in seen:
                continue
            seen.add(issue.id)
            issues.append(issue)
    issues.sort(key=lambda i: (SEVERITY_RANK.get(i.severity, 9), i.resource.get('name') or '', i.title))
    return issues


# ---------------------------------------------------------------------------
# Alerts (critical only, one message per issue, one when it clears)
# ---------------------------------------------------------------------------

def alert_actions(findings: List[dict], now: datetime) -> List[tuple]:
    """[('new' | 'resolved', finding)] still to send for these findings rows.

    new: open, critical, alertable, not snoozed, never alerted.
    resolved: resolved and its "new" alert went out, "resolved" not yet.
    """
    out = []
    for f in findings:
        if f.get('resolved_at') is None:
            snoozed = utc(f.get('snoozed_until'))
            if (f.get('severity') == SEV_CRITICAL and f.get('alert') and not f.get('alerted_at')
                    and not (snoozed and snoozed > now)):
                out.append(('new', f))
        elif f.get('alerted_at') and not f.get('resolved_alerted_at'):
            out.append(('resolved', f))
    return out


def summarize(issues: List[dict], snoozed_count: int = 0) -> dict:
    counts = {s: 0 for s in SEVERITIES}
    for i in issues:
        counts[i['severity']] = counts.get(i['severity'], 0) + 1
    status = 'critical' if counts[SEV_CRITICAL] else ('warning' if counts[SEV_WARNING] else 'ok')
    return {'status': status, 'total': len(issues), 'critical': counts[SEV_CRITICAL],
            'warning': counts[SEV_WARNING], 'info': counts[SEV_INFO], 'snoozed': snoozed_count}


def is_due(last, now, every_seconds) -> bool:
    t = utc(last)
    return t is None or (now - t) >= timedelta(seconds=every_seconds)

"""
Point-in-time recovery: database rows and the runs backupx does over SSH.

Pure decisions live in app.pitr, shell in app.pitr_scripts. This module glues
them to the database and to app.main's SSH / restic helpers (imported lazily:
app.main imports this module).

Scheduler: each PITR job gets an interval entry `<job>__pitr` on its own
APScheduler executor 'pitr' (PITR_MAX_CONCURRENT threads), so shipping never
waits behind the backup pool. A tick holds the advisory lock "<job>:pitr";
the job's backup lock is separate, so the nightly dump and shipping run side
by side. Every tick borrows pooled DB connections per query
(main._short_lived_db), like backup runs.
"""

import json
import logging
import subprocess
import threading
from datetime import datetime, timedelta, timezone

from . import pitr
from . import pitr_scripts as scripts

logger = logging.getLogger(__name__)


class PitrError(Exception):
    pass


def _m():
    from . import main
    return main


def _now():
    return datetime.now(timezone.utc)


def _ts(value):
    """DB timestamp (naive UTC) for a datetime/epoch."""
    if value is None:
        return None
    value = pitr._utc(value)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value):
    value = pitr._utc(value) if value is not None else None
    return value.isoformat() if value else None


def _json(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def lock_key(job_id):
    return f'{job_id}:pitr'


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _db():
    return _m().get_db_connection()


def load_state(job_id):
    row = _db().fetchone('SELECT * FROM pitr_state WHERE job_id = ?', (job_id,))
    return dict(row) if row else {}


def save_state(job_id, **fields):
    conn = _db()
    fields['updated_at'] = _ts(_now())
    exists = conn.fetchone('SELECT 1 FROM pitr_state WHERE job_id = ?', (job_id,))
    cols = sorted(fields)
    if exists:
        conn.execute(f"UPDATE pitr_state SET {', '.join(c + '=?' for c in cols)} WHERE job_id=?",
                     tuple(fields[c] for c in cols) + (job_id,))
    else:
        conn.execute(f"INSERT INTO pitr_state (job_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
                     (job_id,) + tuple(fields[c] for c in cols))
    conn.commit()


def load_chains(job_id):
    rows = _db().fetchall('SELECT * FROM pitr_chains WHERE job_id = ? ORDER BY chain_id', (job_id,))
    return [dict(r) for r in rows]


def _segment_row(row):
    seg = dict(row)
    seg['snapshots'] = _json(seg.get('snapshots'), {})
    seg['end_time'] = pitr._utc(seg.get('end_time'))
    return seg


def load_segments(job_id, kind=None, include_forgotten=True):
    sql = 'SELECT * FROM pitr_segments WHERE job_id = ?'
    params = [job_id]
    if kind:
        sql += ' AND kind = ?'
        params.append(kind)
    if not include_forgotten:
        sql += ' AND NOT forgotten'
    rows = _db().fetchall(sql + ' ORDER BY id', tuple(params))
    return [_segment_row(r) for r in rows]


def load_anchors(job_id):
    rows = _db().fetchall('SELECT * FROM pitr_anchors WHERE job_id = ? ORDER BY time', (job_id,))
    anchors = []
    for row in rows:
        a = dict(row)
        a['snapshots'] = _json(a.get('snapshots'), {})
        a['time'] = pitr._utc(a.get('time'))
        anchors.append(a)
    return anchors


def delete_job_rows(job_id):
    conn = _db()
    for table in ('pitr_state', 'pitr_chains', 'pitr_segments', 'pitr_anchors'):
        conn.execute(f'DELETE FROM {table} WHERE job_id = ?', (job_id,))
    conn.commit()


def _storage_key(target):
    return target.get('id') or ''


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _job_context(job_id, job=None):
    """(job, server, db_config, kind) or raise PitrError."""
    m = _m()
    job = job or m.get_job(job_id)
    if not job:
        raise PitrError('job not found')
    job = dict(job, id=job_id)
    if job.get('backup_type') != 'database':
        raise PitrError('point-in-time recovery is only for database jobs')
    db_config = m.get_db_config(job.get('database_config_id')) if job.get('database_config_id') else None
    if not db_config:
        raise PitrError('database configuration not found')
    server = m.get_server(job['server_id']) if job.get('server_id') else None
    if not server:
        raise PitrError('PITR needs the job to use a configured server (the streamer runs there)')
    return job, server, db_config, pitr.job_kind(db_config)


def binlog_job(job):
    """The job as seen by the restic helpers, pointed at the sibling binlog repository."""
    prefix = (job.get('backup_prefix') or job.get('id') or '') + pitr.BINLOG_REPO_SUFFIX
    return dict(job, backup_prefix=prefix)


def _ssh(server, timeout=30):
    return _m()._build_ssh_cmd_for_server(server, timeout=timeout)


def _run(server, script, timeout):
    return _m()._ssh_run(_ssh(server), script, timeout=timeout)


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def check_prereqs(job_id, job=None, overrides=None):
    """Run the prerequisite check for a job; returns {'kind', 'checks', 'ok', 'needs_restart', ...}."""
    job, server, db_config, kind = _job_context(job_id, job)
    if overrides:
        job.update({k: v for k, v in overrides.items() if k.startswith('pitr_')})
    if kind == 'postgres':
        res = _run(server, scripts.pg_prereq_script(), timeout=90)
        facts = pitr.parse_kv_output(res.stdout)
        checks = pitr.evaluate_pg_prereqs(facts)
        extra = {'postgresql_conf': pitr.postgres_conf_changes()}
    else:
        image = (job.get('pitr_client_image') or '').strip()
        res = _run(server, scripts.mysql_prereq_script(db_config, image), timeout=180)
        facts = pitr.parse_kv_output(res.stdout)
        mode = 'docker' if (db_config.get('docker_container') or '').strip() else 'direct'
        checks = pitr.evaluate_mysql_prereqs(facts, mode=mode, client_image=image)
        extra = {'server_version': facts.get('version', ''),
                 'suggested_client_image': pitr.suggested_client_image(facts.get('version', ''))}
    if res.returncode != 0 and not facts:
        checks.insert(0, {'check': 'SSH', 'status': 'fail',
                          'current': _m().sanitize_error_message(res.stderr.strip() or 'ssh failed'),
                          'required': 'backupx can run commands on the server', 'fix': '', 'needs_restart': False})
    return dict(extra, kind=kind, checks=checks, ok=pitr.prereqs_ok(checks),
                needs_restart=any(c['needs_restart'] and c['status'] != 'ok' for c in checks))


# ---------------------------------------------------------------------------
# Anchor (called by the database backup run)
# ---------------------------------------------------------------------------

def record_anchor(job_id, job, anchor, anchor_error, targets, results):
    """Store the dump's binlog position after a backup. Never raises."""
    try:
        snapshots = {_storage_key(t): r.get('snapshot_id') for t, r in zip(targets, results)
                     if r.get('exit_code') == 0 and r.get('snapshot_id')}
        if anchor is None:
            message = f"dump has no binlog position: {anchor_error or 'unknown reason'}"
            logger.warning(f"PITR: job {job.get('name')}: {message}")
            save_state(job_id, last_error=_m().sanitize_error_message('Anchor: ' + message))
            return
        if not snapshots:
            return
        state = load_state(job_id)
        conn = _db()
        conn.execute('''
            INSERT INTO pitr_anchors (job_id, time, binlog_file, binlog_pos, gtid_set, server_uuid,
                                      snapshots, forgotten, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, FALSE, ?)
        ''', (job_id, _ts(anchor.get('time') or _now()), anchor['file'], anchor['pos'],
              (anchor.get('gtid_set') or '')[:10000], state.get('server_uuid') or '',
              json.dumps(snapshots), _ts(_now())))
        conn.commit()
        logger.info(f"PITR: job {job.get('name')}: anchor {anchor['file']}:{anchor['pos']} "
                    f"on {len(snapshots)} storage(s)")
        refresh_state(job_id, job)
    except Exception as e:
        logger.exception(f"PITR: recording the anchor for job {job_id} failed: {e}")


# ---------------------------------------------------------------------------
# Tick: ensure streamer, ship, update state
# ---------------------------------------------------------------------------

def run_tick(job_id):
    """Scheduled every pitr_interval minutes. Never raises."""
    m = _m()
    with m._short_lived_db():
        try:
            from .scheduler.locks import job_run_lock
            with job_run_lock(m._lock_connection, lock_key(job_id)) as acquired:
                if not acquired:
                    logger.info(f"PITR tick skipped, already running: {job_id}")
                    return False, 'already running'
                return _tick(job_id)
        except Exception as e:
            logger.exception(f"PITR tick for job {job_id} failed")
            try:
                _record_failure(job_id, f'{type(e).__name__}: {e}')
            except Exception:
                pass
            return False, str(e)
        finally:
            try:
                m.get_db().close()
            except Exception:
                pass


def _record_failure(job_id, message):
    m = _m()
    message = m.sanitize_error_message(message)
    state = load_state(job_id)
    save_state(job_id, last_error=message, last_tick_at=_ts(_now()),
               consecutive_failures=int(state.get('consecutive_failures') or 0) + 1)
    job = m.get_job(job_id)
    if job:
        refresh_state(job_id, dict(job, id=job_id))


def _tick(job_id):
    job, server, db_config, kind = _job_context(job_id)
    if not job.get('pitr_enabled'):
        return False, 'PITR is off for this job'
    targets = _m().get_job_storage_targets(job)
    if not targets:
        raise PitrError('no active storage')
    if kind == 'postgres':
        return _pg_tick(job_id, job, server, db_config, targets)
    return _mysql_tick(job_id, job, server, db_config, targets)


def _mysql_tick(job_id, job, server, db_config, targets):
    m = _m()
    cfg = pitr.settings()
    res = _run(server, scripts.ensure_and_scan_script(job_id, db_config, job.get('pitr_client_image') or ''),
               timeout=180)
    scan = pitr.parse_scan_output(res.stdout)
    if scan['errors'] or res.returncode != 0:
        raise PitrError('; '.join(scan['errors']) or res.stderr.strip() or 'streamer setup failed')
    state = load_state(job_id)
    plan = pitr.plan_shipment(state, scan)
    now = pitr._utc(scan['now']) if scan['now'] else _now()
    in_sync, alive = pitr.streamer_health(scan['active'], scan['heartbeat'],
                                          scan['now'] or int(_now().timestamp()), cfg['heartbeat_max_age'])

    shipped = None
    message = ''
    if plan['segments'] or plan['partial'] or plan['stale'] or plan['ack_events']:
        if not m.ensure_restic_installed(server['host'], server.get('ssh_user', 'root'),
                                         int(server.get('ssh_port') or 22), m._get_ssh_key_path(server),
                                         ssh_auth_type=server.get('ssh_auth_type', 'key_path'),
                                         ssh_password=_server_password(server))[0]:
            raise PitrError('restic is not available on the streamer host')
        bjob = binlog_job(job)
        script = scripts.ship_script(job_id, plan['segments'], plan['partial'], plan['stale'],
                                     plan['ack_events'], job['restic_password'],
                                     m._restic_target_blocks(bjob, targets, 'pitr_ship'))
        res2 = _run(server, script, timeout=cfg['ship_timeout'])
        results, confirmed = pitr.parse_ship_output(res2.stdout, len(targets))
        ok = res2.returncode == 0 and confirmed and all(r['exit_code'] == 0 for r in results)
        if not ok:
            failed = [f"{t['name']}: {' '.join(r['output'][-3:]) or 'exit ' + str(r['exit_code'])}"
                      for t, r in zip(targets, results) if r['exit_code'] != 0]
            _forget_orphans(bjob, targets, results)
            raise PitrError('shipping failed - ' + ('; '.join(failed) or res2.stderr.strip()[-300:]
                                                     or 'unknown error'))
        shipped = results

    _apply_plan(job_id, state, plan, scan, targets, shipped, in_sync, now)
    if plan['segments']:
        message = (f"shipped {sum(len(s['files']) for s in plan['segments'])} binlog file(s) "
                   f"up to {plan['segments'][-1]['last_file']}")
    _gc_partials(job_id, job, targets)
    refresh_state(job_id, job, live=alive)
    return True, message or 'nothing new to ship'


def _server_password(server):
    m = _m()
    password = server.get('ssh_password', '') or ''
    if password and m.is_encrypted(password):
        password = m.decrypt_credential(password)
    return password


def _forget_orphans(bjob, targets, results):
    """A shipment that failed somewhere is retried in full; drop the copies that did land."""
    m = _m()
    for target, r in zip(targets, results):
        ids = [i for i in list(r['segments'].values()) + [r['partial']] if i]
        if not ids or r['exit_code'] != 0:
            continue
        try:
            m._restic_local(bjob, target, ['forget', *ids], 300, '2m')
        except Exception as e:
            logger.warning(f"PITR: couldn't forget orphan snapshots on {target.get('name')}: {e}")


def _apply_plan(job_id, state, plan, scan, targets, shipped, in_sync, now):
    conn = _db()
    for br in plan['breaks']:
        conn.execute('''
            INSERT INTO pitr_chains (job_id, chain_id, start_seq, server_uuid, base, reason, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, chain_id) DO NOTHING
        ''', (job_id, br['chain_id'], br['start_seq'], br['server_uuid'], br['base'], br['reason'][:500],
              _ts(now)))
        if br['reason'] != 'start':
            logger.warning(f"PITR: job {job_id}: chain broken - {br['reason']}")
    if plan['server_uuid']:
        conn.execute("UPDATE pitr_anchors SET server_uuid = ? WHERE job_id = ? AND server_uuid = ''",
                     (plan['server_uuid'], job_id))
    for index, seg in enumerate(plan['segments']):
        snaps = {_storage_key(t): r['segments'].get(index) for t, r in zip(targets, shipped or [])}
        conn.execute('''
            INSERT INTO pitr_segments (job_id, chain_id, kind, first_file, last_file, first_seq, last_seq,
                                       end_time, size, snapshots, forgotten, created_at)
            VALUES (?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, FALSE, ?)
        ''', (job_id, seg['chain_id'], seg['first_file'], seg['last_file'], seg['first_seq'], seg['last_seq'],
              _ts(seg['end_time']), seg['size'], json.dumps(snaps), _ts(now)))
    fields = {
        'kind': 'mysql', 'chain_id': plan['chain_id'], 'server_uuid': plan['server_uuid'] or None,
        'binlog_base': plan['base'] or None, 'last_seq': plan['last_seq'],
        'streamer_active': scan['active'], 'heartbeat': _ts(scan['heartbeat']) if scan['heartbeat'] else None,
        'server_version': scan['server_version'] or state.get('server_version'),
        'last_tick_at': _ts(now), 'last_error': None, 'consecutive_failures': 0,
    }
    if plan['segments']:
        fields['last_ship_at'] = _ts(now)
    open_file = plan['open']
    if plan['partial']:
        p = plan['partial']
        snaps = {_storage_key(t): r['partial'] for t, r in zip(targets, shipped or []) if r['partial']}
        conn.execute('''
            INSERT INTO pitr_segments (job_id, chain_id, kind, first_file, last_file, first_seq, last_seq,
                                       end_time, size, snapshots, forgotten, created_at)
            VALUES (?, ?, 'partial', ?, ?, ?, ?, ?, ?, ?, FALSE, ?)
        ''', (job_id, plan['chain_id'], p['name'], p['name'], pitr.binlog_seq(p['name']),
              pitr.binlog_seq(p['name']), _ts(p['mtime']), p['size'], json.dumps(snaps), _ts(now)))
        fields.update(partial_file=p['name'], partial_size=p['size'], partial_mtime=p['mtime'],
                      partial_until=_ts(now if in_sync else p['mtime']))
    elif open_file and open_file['name'] == state.get('partial_file') and in_sync:
        # Unchanged since the last copy and the stream is in sync: still complete up to now
        fields['partial_until'] = _ts(now)
    elif not open_file:
        fields.update(partial_file=None, partial_size=None, partial_mtime=None, partial_until=None)
    conn.commit()
    save_state(job_id, **fields)


def _gc_partials(job_id, job, targets):
    """Forget superseded open-file copies (keep the newest), at most every 30 minutes."""
    state = load_state(job_id)
    info = _json(state.get('info'), {})
    last = pitr._utc(info.get('partial_gc_at')) if info.get('partial_gc_at') else None
    if last and _now() - last < timedelta(minutes=30):
        return
    partials = [p for p in load_segments(job_id, kind='partial') if not p.get('forgotten')]
    if len(partials) <= 1:
        return
    newest = max(partials, key=lambda p: p['id'])
    old = [p for p in partials if p['id'] != newest['id']]
    bjob = binlog_job(job)
    m = _m()
    for target in targets:
        ids = [p['snapshots'].get(_storage_key(target)) for p in old]
        ids = [i for i in ids if i]
        if not ids:
            continue
        proc = m._restic_local(bjob, target, ['forget', *ids], 600, '5m')
        if proc.returncode != 0:
            logger.warning(f"PITR: forgetting old partial copies on {target.get('name')} failed: "
                           f"{m._restic_error(proc, 'forget')}")
            return
    conn = _db()
    for p in old:
        conn.execute('UPDATE pitr_segments SET forgotten = TRUE WHERE id = ?', (p['id'],))
    conn.commit()
    info['partial_gc_at'] = _now().isoformat()
    save_state(job_id, info=json.dumps(info))


# ---------------------------------------------------------------------------
# State, window, alerts
# ---------------------------------------------------------------------------

def _partial_for_window(state):
    if not state.get('partial_file'):
        return None
    return {'chain_id': state.get('chain_id'), 'seq': pitr.binlog_seq(state['partial_file']),
            'until': state.get('partial_until')}


def compute_windows(job_id, state=None):
    state = state if state is not None else load_state(job_id)
    chains = load_chains(job_id)
    segments = [s for s in load_segments(job_id, kind='closed')]
    anchors = load_anchors(job_id)
    return pitr.chain_windows(chains, segments, anchors, _partial_for_window(state))


def refresh_state(job_id, job, live=None):
    """Recompute window + state from the rows, alert on transitions. Never raises."""
    try:
        state = load_state(job_id)
        kind = state.get('kind') or 'mysql'
        cfg = pitr.settings()
        enabled = bool(job.get('pitr_enabled'))
        if kind == 'postgres':
            windows = _pg_windows(state)
        else:
            windows = compute_windows(job_id, state)
        if live is None:
            if kind == 'postgres':
                live = state.get('streamer_active') in (None, 'active')
            else:
                hb = pitr._utc(state['heartbeat']).timestamp() if state.get('heartbeat') else None
                live = pitr.streamer_health(state.get('streamer_active') or 'active', hb,
                                            _now().timestamp(), cfg['heartbeat_max_age'])[1]
        lag_limit = pitr.lag_threshold_minutes(job.get('pitr_interval'), cfg['lag_minutes'], kind)
        new_state, window, lag = pitr.derive_state(enabled, bool(state.get('last_tick_at')), windows,
                                                   _now(), lag_limit, live)
        fields = {
            'state': new_state,
            'window_start': _ts(window['from']) if window and window.get('from') else None,
            'window_end': _ts(window['to']) if window and window.get('to') else None,
            'lag_minutes': round(lag, 1) if lag is not None else None,
        }
        if new_state != state.get('state'):
            fields['state_since'] = _ts(_now())
        alerted = state.get('alerted_state')
        kind_alert, new_alerted = pitr.alert_decision(new_state, alerted)
        if kind_alert == 'failed' and new_state == pitr.STATE_UNANCHORED and alerted is None:
            # Expected right after enabling, until the first dump: alert only if it lasts
            since = fields.get('state_since') or state.get('state_since')
            if not pitr.should_alert_unanchored(since, _now()):
                kind_alert, new_alerted = None, None
        fields['alerted_state'] = new_alerted
        save_state(job_id, **fields)
        if kind_alert:
            _notify(job_id, job, kind_alert, new_state, state, window, lag, windows)
        return new_state
    except Exception as e:
        logger.exception(f"PITR: refreshing state for job {job_id} failed: {e}")
        return None


def _describe_window(window):
    if not window or not window.get('from'):
        return 'nothing restorable'
    return f"restorable {window['from'].strftime('%Y-%m-%d %H:%M')} .. {window['to'].strftime('%Y-%m-%d %H:%M')} UTC"


def _notify(job_id, job, kind_alert, new_state, old, window, lag, windows):
    m = _m()
    name = f"{job.get('name') or job_id} (PITR)"
    if kind_alert == 'recovered':
        message = f"Point-in-time recovery is OK again: {_describe_window(window)}"
        status = 'recovered'
    else:
        status = 'failed'
        if new_state == pitr.STATE_LAGGING:
            message = (f"Point-in-time recovery is lagging: newest restorable time is "
                       f"{round(lag) if lag is not None else '?'} min old")
            if old.get('last_error'):
                message += f". Last error: {old['last_error']}"
        elif new_state == pitr.STATE_BROKEN:
            reason = windows[-1]['reason'] if windows else 'unknown'
            message = (f"Point-in-time recovery chain broken ({reason}); times after the break are not "
                       f"restorable until the next full backup. Earlier: {_describe_window(window)}")
        else:
            message = "Point-in-time recovery has no usable full backup (anchor) yet"
            if old.get('last_error'):
                message += f": {old['last_error']}"
    try:
        m.send_notification(job_id, name, status, m.sanitize_error_message(message, 1000), 0)
    except Exception as e:
        logger.error(f"PITR: notification for job {job_id} failed: {e}")


def status(job_id, job):
    """Everything the UI shows for one job."""
    state = load_state(job_id)
    out = {
        'enabled': bool(job.get('pitr_enabled')),
        'state': state.get('state') or (pitr.STATE_PENDING if job.get('pitr_enabled') else pitr.STATE_DISABLED),
        'state_since': _iso(state.get('state_since')),
        'window_start': _iso(state.get('window_start')),
        'window_end': _iso(state.get('window_end')),
        'lag_minutes': state.get('lag_minutes'),
        'last_tick_at': _iso(state.get('last_tick_at')),
        'last_ship_at': _iso(state.get('last_ship_at')),
        'last_error': state.get('last_error'),
        'streamer_active': state.get('streamer_active'),
        'server_version': state.get('server_version'),
        'kind': state.get('kind'),
    }
    return out


def summaries(jobs):
    """{job_id: status} for every PITR-enabled job (one query)."""
    rows = _db().fetchall('SELECT * FROM pitr_state')
    by_id = {r['job_id']: dict(r) for r in rows}
    out = {}
    for job_id, job in jobs.items():
        if not job.get('pitr_enabled') and job_id not in by_id:
            continue
        s = by_id.get(job_id, {})
        out[job_id] = {
            'enabled': bool(job.get('pitr_enabled')),
            'state': s.get('state') or (pitr.STATE_PENDING if job.get('pitr_enabled') else pitr.STATE_DISABLED),
            'window_start': _iso(s.get('window_start')),
            'window_end': _iso(s.get('window_end')),
            'lag_minutes': s.get('lag_minutes'),
            'last_error': s.get('last_error'),
        }
    return out


def details(job_id, job):
    out = status(job_id, job)
    out['chains'] = [dict(c, started_at=_iso(c.get('started_at'))) for c in load_chains(job_id)]
    windows = compute_windows(job_id) if (out.get('kind') or 'mysql') == 'mysql' else []
    out['windows'] = [dict(w, **{'from': _iso(w['from']), 'to': _iso(w['to'])}) for w in windows]
    anchors = load_anchors(job_id)[-20:]
    out['anchors'] = [{'id': a['id'], 'time': _iso(a['time']), 'binlog_file': a['binlog_file'],
                       'binlog_pos': a['binlog_pos'], 'storages': sorted(a['snapshots']),
                       'forgotten': bool(a.get('forgotten'))} for a in anchors]
    segs = load_segments(job_id, kind='closed')[-20:]
    out['segments'] = [{'id': s['id'], 'chain_id': s['chain_id'], 'first_file': s['first_file'],
                        'last_file': s['last_file'], 'end_time': _iso(s['end_time']), 'size': s['size'],
                        'forgotten': bool(s.get('forgotten'))} for s in segs]
    return out


# ---------------------------------------------------------------------------
# Restore script
# ---------------------------------------------------------------------------

def restore_script(job_id, target_time, storage_id=None):
    """(filename, script text) restoring the job to target_time. Raises PitrError / RestorePlanError."""
    m = _m()
    job, server, db_config, kind = _job_context(job_id)
    target_time = pitr._utc(target_time)
    storage = m.resolve_job_storage(job, storage_id)
    if not storage:
        raise PitrError('storage not found')
    stamp = target_time.strftime('%Y%m%dT%H%M%SZ')
    if kind == 'postgres':
        text = scripts.pg_restore_script(job, target_time, storage, job.get('backup_prefix') or job_id)
        return f'restore-{job_id}-{stamp}.sh', text
    state = load_state(job_id)
    chains = load_chains(job_id)
    segments = load_segments(job_id, kind='closed')
    partials = [p for p in load_segments(job_id, kind='partial') if not p.get('forgotten')]
    if partials and state.get('partial_until'):
        newest = max(partials, key=lambda p: p['id'])
        newest['until'] = state['partial_until']
    plan = pitr.plan_restore(target_time, chains, segments, load_anchors(job_id), _storage_key(storage), partials)
    version = state.get('server_version') or ''
    server_image = pitr.scratch_server_image(version)
    binlog_image = pitr.suggested_client_image(version) or 'percona/percona-server:8.4'
    main_repo = m.job_repo_url(job, storage)
    binlog_repo = m.job_repo_url(binlog_job(job), storage)
    text = scripts.mysql_restore_script(job, plan, target_time, storage, main_repo, binlog_repo, server_image,
                                        binlog_image, pitr.server_flavor(version))
    return f'restore-{job_id}-{stamp}.sh', text


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def teardown(job_id, job, keep_state=True):
    """Stop the streamer when PITR is switched off (background, never raises).

    Shipped data and the spool on the host are kept; the credentials file is removed.
    """
    def work():
        m = _m()
        with m._short_lived_db():
            try:
                server = m.get_server(job['server_id']) if job.get('server_id') else None
                db_config = m.get_db_config(job.get('database_config_id')) if job.get('database_config_id') else None
                if server and pitr.job_kind(db_config) == 'mysql':
                    res = _run(server, scripts.teardown_script(job_id), timeout=120)
                    if 'PITR\tSTOPPED\t1' not in res.stdout:
                        logger.warning(f"PITR: stopping the streamer of {job_id} may have failed: "
                                       f"{res.stderr.strip()[-200:]}")
                if keep_state:
                    save_state(job_id, state=pitr.STATE_DISABLED, state_since=_ts(_now()), alerted_state=None)
            except Exception as e:
                logger.error(f"PITR: teardown for job {job_id} failed: {e}")
            finally:
                try:
                    m.get_db().close()
                except Exception:
                    pass
    threading.Thread(target=work, name=f'pitr-teardown-{job_id}', daemon=True).start()


def kick(job_id):
    """Run a tick now in the background (after enabling, or 'Ship now')."""
    threading.Thread(target=run_tick, args=(job_id,), name=f'pitr-{job_id}', daemon=True).start()


# ---------------------------------------------------------------------------
# Retention by anchor (weekly, with the restic prune)
# ---------------------------------------------------------------------------

def apply_retention(job_id, job, cfg):
    """Forget shipped binlogs older than the oldest usable dump within pitr_retention_days,
    then prune the binlog repositories. Runs from the weekly maintenance. Never raises."""
    m = _m()
    try:
        job = dict(job, id=job_id)
        db_config = m.get_db_config(job.get('database_config_id')) if job.get('database_config_id') else None
        if pitr.job_kind(db_config) == 'postgres':
            return _pg_retention(job_id, job, cfg)
        targets = m.get_job_storage_targets(job)
        if not targets:
            return
        _refresh_anchor_snapshots(job_id, job, targets)
        chains, segments, anchors = load_chains(job_id), load_segments(job_id, kind='closed'), load_anchors(job_id)
        plan = pitr.retention_plan(chains, segments, anchors, _now(), job.get('pitr_retention_days'))
        bjob = binlog_job(job)
        if not plan['forget']:
            message = ('nothing to forget' if plan['cutoff_anchor'] else
                       'no full backup within the PITR period; keeping every binlog')
            for target in targets:
                m.record_retention_run(job_id, target, 'pitr-forget', 'success', message, 0, dry_run=cfg['dry_run'])
        else:
            for target in targets:
                ids = [s['snapshots'].get(_storage_key(target)) for s in plan['forget']]
                ids = [i for i in ids if i]
                cutoff = plan['cutoff_anchor']
                message = (f"{'dry run: would forget' if cfg['dry_run'] else 'forgot'} {len(ids)} binlog "
                           f"snapshot(s) older than the dump at {cutoff['binlog_file']}:{cutoff['binlog_pos']} "
                           f"({_iso(cutoff['time'])})")
                if ids and not cfg['dry_run']:
                    proc = m._restic_local(bjob, target, ['forget', *ids], cfg['forget_timeout'], cfg['lock_wait'])
                    if proc.returncode != 0:
                        raise PitrError(m._restic_error(proc, 'forget'))
                m.record_retention_run(job_id, target, 'pitr-forget', 'success', message, len(ids),
                                       dry_run=cfg['dry_run'])
            if not cfg['dry_run']:
                conn = _db()
                for seg in plan['forget']:
                    conn.execute('UPDATE pitr_segments SET forgotten = TRUE WHERE id = ?', (seg['id'],))
                conn.commit()
        for target in targets:
            proc = m._restic_local(bjob, target, m.retention.prune_args(cfg['dry_run'], cfg['prune_max_unused']),
                                   cfg['prune_timeout'], cfg['lock_wait'])
            if proc.returncode != 0:
                m.record_retention_run(job_id, target, 'pitr-prune', 'failed', m._restic_error(proc, 'prune'),
                                       dry_run=cfg['dry_run'])
            else:
                parsed = m.retention.parse_prune_output(proc.stdout)
                m.record_retention_run(job_id, target, 'pitr-prune', 'success', parsed['summary'] or 'pruned',
                                       bytes_freed=parsed['bytes_freed'], dry_run=cfg['dry_run'])
        refresh_state(job_id, job)
    except Exception as e:
        message = m.sanitize_error_message(f'PITR retention failed: {e}')
        logger.error(f"Job {job.get('name')}: {message}")
        try:
            m.send_notification(job_id, job.get('name') or job_id, 'failed', message, 0)
        except Exception:
            pass


def _refresh_anchor_snapshots(job_id, job, targets):
    """Mark anchors whose dump was forgotten by the job's own retention as unusable."""
    m = _m()
    anchors = [a for a in load_anchors(job_id) if not a.get('forgotten')]
    if not anchors:
        return
    conn = _db()
    for target in targets:
        key = _storage_key(target)
        try:
            m.invalidate_snapshot_cache(job_id)
            existing = {s.get('id') for s in m.get_snapshots(job, target)}
            existing |= {s.get('short_id') for s in m.get_snapshots(job, target)}
        except Exception as e:
            logger.warning(f"PITR: can't list snapshots on {target.get('name')}: {e}")
            return  # unknown: change nothing
        for a in anchors:
            snap = a['snapshots'].get(key)
            if snap and snap not in existing:
                a['snapshots'].pop(key)
                conn.execute('UPDATE pitr_anchors SET snapshots = ?, forgotten = ? WHERE id = ?',
                             (json.dumps(a['snapshots']), not a['snapshots'], a['id']))
    conn.commit()


# ---------------------------------------------------------------------------
# PostgreSQL / WAL-G
# ---------------------------------------------------------------------------
# Phase 2. Implemented: prerequisite check, wal-g install (binary + 0600 env +
# archive script; postgresql.conf changes are only printed), nightly
# backup-push on the job's schedule, archiver + wal-verify monitoring, the R2
# mirror (rclone copy, never sync) and the restore script.
# TODO(pitr-pg): end-to-end test against the DSpace host after the restart window;
# R2 copy pruning (bucket lifecycle rule for now, see docs/PITR.md).

def _pg_windows(state):
    info = _json(state.get('info'), {})
    start = info.get('oldest_base_backup')
    end = info.get('last_archived_time')
    reason = 'start'
    if info.get('integrity') == 'FAILURE':
        reason = 'wal-verify integrity: ' + (info.get('integrity_detail') or 'FAILURE')
    if not end:
        return [{'chain_id': 1, 'reason': reason, 'from': None, 'to': None, 'anchored': False, 'anchor_ids': []}]
    to = pitr._utc(int(end))
    frm = pitr._utc(start) if start else None
    anchored = bool(frm and frm <= to and reason == 'start')
    # idle_at: the last tick that found nothing written since the last archived WAL;
    # the window is current up to then even though the archive time is old.
    return [{'chain_id': 1, 'reason': reason, 'from': frm if anchored else None, 'to': to,
             'anchored': anchored, 'anchor_ids': [], 'complete_at': info.get('idle_at')}]


def _pg_tick(job_id, job, server, db_config, targets):
    m = _m()
    state = load_state(job_id)
    info = _json(state.get('info'), {})
    primary = targets[0]
    prefix = job.get('backup_prefix') or job_id
    env_hash = _hash(primary.get('endpoint'), primary.get('bucket'), primary.get('access_key'),
                     primary.get('secret_key'), prefix)
    if info.get('walg_env_hash') != env_hash or not info.get('walg_version'):
        res = _run(server, scripts.walg_install_script(primary, prefix), timeout=300)
        facts = pitr.parse_kv_output(res.stdout)
        if 'INSTALLED' not in facts:
            raise PitrError(facts.get('ERROR') or res.stderr.strip()[-300:] or 'wal-g install failed')
        info.update(walg_env_hash=env_hash, walg_version=facts.get('WALG_VERSION', ''),
                    pgdata=facts.get('PGDATA', ''))
    res = _run(server, scripts.pg_prereq_script(), timeout=90)
    facts = pitr.parse_kv_output(res.stdout)
    if facts.get('connect_error'):
        raise PitrError('psql: ' + facts['connect_error'])
    archiving = facts.get('archive_mode') in ('on', 'always') and \
        pitr.WALG_ARCHIVE_COMMAND.split()[0] in facts.get('archive_command', '')
    health = pitr.walg_archiver_health(facts)
    now = _now()
    info.update(archive_mode=facts.get('archive_mode'), archiving=archiving,
                last_archived_wal=facts.get('archiver_last_archived_wal'),
                last_archived_time=facts.get('archiver_last_archived_time') or None,
                archiver_failed_count=facts.get('archiver_failed_count'),
                current_wal_file=facts.get('current_wal_file') or None,
                archiver=health['detail'],
                idle_at=now.isoformat() if archiving and health['idle'] else None)
    message = None if archiving else ('postgresql.conf changes pending (archive_mode/archive_command); '
                                      'see Check prerequisites')
    if archiving and health['failing']:
        message = health['detail']
    ship_ok = archiving and not health['failing']
    if archiving:
        res = _run(server, scripts.walg_run_script('wal-g backup-list --json --detail'), timeout=120)
        try:
            backups = json.loads(res.stdout.strip() or '[]') or []
        except ValueError:
            backups = []
        times = sorted(b.get('start_time') for b in backups if b.get('start_time'))
        info['oldest_base_backup'] = times[0] if times else None
        info['base_backups'] = len(backups)
        last_verify = info.get('verify_at')
        if not last_verify or _now() - pitr._utc(last_verify) > timedelta(hours=1):
            res = _run(server, scripts.walg_run_script('wal-g wal-verify integrity timeline --json'), timeout=600)
            try:
                verdict = json.loads(res.stdout.strip().splitlines()[-1])
                integrity = verdict.get('integrity', {})
                info['integrity'] = integrity.get('status')
                info['integrity_detail'] = json.dumps(integrity.get('details'))[:300]
                info['timeline'] = verdict.get('timeline', {}).get('status')
            except (ValueError, IndexError, AttributeError):
                info['integrity'] = None
            info['verify_at'] = _now().isoformat()
        if len(targets) > 1:
            last_mirror = info.get('mirror_at')
            if not last_mirror or _now() - pitr._utc(last_mirror) >= timedelta(minutes=15):
                info['mirror'] = _mirror_wal(prefix, primary, targets[1:])
                info['mirror_at'] = _now().isoformat()
                failed = [f"{r['storage']}: {r['error'] or 'failed'}" for r in info['mirror'] if not r['ok']]
                if failed:
                    ship_ok = False
                    message = message or ('WAL mirror failed - ' + '; '.join(failed))[:500]
            else:
                ship_ok = False  # nothing shipped to the other storages this tick
    fields = {}
    if ship_ok:
        # A "ship": WAL on the primary is current (archiver healthy) and every storage
        # has it - even when an idle server produced no new files.
        fields['last_ship_at'] = _ts(now)
    save_state(job_id, kind='postgres', info=json.dumps(info), last_tick_at=_ts(now),
               last_error=message, consecutive_failures=0,
               streamer_active=('failing' if health['failing'] else 'active') if archiving else 'inactive',
               server_version=facts.get('server_version'), **fields)
    refresh_state(job_id, job, live=archiving and not health['failing'])
    return True, message or ('ok (' + health['detail'] + ')' if archiving else 'ok')


def _hash(*parts):
    import hashlib
    return hashlib.sha256('\0'.join(str(p or '') for p in parts).encode()).hexdigest()[:16]


def _rclone_env(name, target):
    m = _m()
    prefix = f'RCLONE_CONFIG_{name.upper()}_'
    return {
        prefix + 'TYPE': 's3', prefix + 'PROVIDER': 'Other',
        prefix + 'ACCESS_KEY_ID': target.get('access_key') or '',
        prefix + 'SECRET_ACCESS_KEY': target.get('secret_key') or '',
        prefix + 'ENDPOINT': m.s3_endpoint_url(target['endpoint']),
        prefix + 'FORCE_PATH_STYLE': 'true',
    }


def _mirror_wal(prefix, primary, others):
    """rclone copy (never sync) of the WAL-G prefix to the other storages, from this container."""
    import os
    results = []
    for target in others:
        env = os.environ.copy()
        env.update(_rclone_env('src', primary))
        env.update(_rclone_env('dst', target))
        if primary.get('skip_ssl_verify') or target.get('skip_ssl_verify'):
            env['RCLONE_NO_CHECK_CERTIFICATE'] = 'true'
        path = f'{prefix}{pitr.WAL_REPO_SUFFIX}'
        try:
            proc = subprocess.run(['rclone', 'copy', '--fast-list', '--checkers', '8',
                                   f"src:{primary['bucket']}/{path}", f"dst:{target['bucket']}/{path}"],
                                  capture_output=True, text=True, env=env, timeout=1800)
            results.append({'storage': target.get('name'), 'ok': proc.returncode == 0,
                            'error': _m().explain_rclone_error(proc.stderr) if proc.returncode else ''})
        except Exception as e:
            results.append({'storage': target.get('name'), 'ok': False, 'error': str(e)[:200]})
    return results


def run_pg_base_backup(job_id):
    """Nightly wal-g backup-push (scheduled with the job's cron). Never raises."""
    m = _m()
    with m._short_lived_db():
        try:
            from .scheduler.locks import job_run_lock
            with job_run_lock(m._lock_connection, f'{job_id}:walg-base') as acquired:
                if not acquired:
                    return False, 'already running'
                job, server, db_config, kind = _job_context(job_id)
                if kind != 'postgres' or not job.get('pitr_enabled'):
                    return False, 'not a PITR PostgreSQL job'
                state = load_state(job_id)
                if not _json(state.get('info'), {}).get('archiving'):
                    return False, 'archiving not enabled yet'
                res = _run(server, scripts.walg_run_script('wal-g backup-push "$PGDATA"'),
                           timeout=int(job.get('timeout') or 7200))
                if res.returncode != 0:
                    raise PitrError(res.stderr.strip()[-400:] or 'wal-g backup-push failed')
                m.add_history(job_id, job['name'], 'success', 'WAL-G base backup (PITR) completed', 0)
                return True, 'ok'
        except Exception as e:
            message = m.sanitize_error_message(f'WAL-G base backup failed: {e}')
            logger.error(f"PITR: job {job_id}: {message}")
            try:
                job = m.get_job(job_id) or {}
                m.add_history(job_id, job.get('name') or job_id, 'failed', message, 0)
                m.send_notification(job_id, f"{job.get('name') or job_id} (PITR)", 'failed', message, 0)
            except Exception:
                pass
            return False, message
        finally:
            try:
                m.get_db().close()
            except Exception:
                pass


def _pg_retention(job_id, job, cfg):
    """wal-g delete before FIND_FULL <now - pitr_retention_days> on the primary storage."""
    m = _m()
    job, server, db_config, kind = _job_context(job_id, job)
    days = int(job.get('pitr_retention_days') or pitr.DEFAULT_RETENTION_DAYS)
    when = (_now() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    confirm = '' if cfg['dry_run'] else ' --confirm'
    res = _run(server, scripts.walg_run_script(f'wal-g delete before FIND_FULL {when}{confirm}'), timeout=3600)
    target = (m.get_job_storage_targets(job) or [{}])[0]
    status_ = 'success' if res.returncode == 0 else 'failed'
    m.record_retention_run(job_id, target, 'pitr-forget', status_,
                           m.sanitize_error_message((res.stdout + res.stderr).strip()[-400:] or 'done'),
                           dry_run=cfg['dry_run'])

"""
Restore wizard: database rows, plans and the restore tasks.

Pure decisions live in app.restore, shell in app.restore_scripts. app.main's
helpers (SSH, storages, run_backup, restic env) are imported lazily (_m()).

Flow (docs/UX-OVERHAUL.md, P2):

1. plan (POST /api/restores/plan): derived from current state - which dump /
   base backup / binlog segments / snapshot, sizes, scratch image, a free-disk
   check on the backupx server (estimated restored size x RESTORE_DISK_FACTOR).
   The hash covers everything but the free-space numbers.
2. scratch restore (task `restore_scratch`, cancellable): restic / wal-g run
   in the backupx container, the scratch server runs on the host's docker
   (socket mounted) as container backupx-restore-<id>, label
   backupx.restore=<id>, no published ports. Then a verification (row
   estimates + exact counts of the job's key tables, prod vs restored).
   Failure or cancel removes the container and the work directory; a dead
   worker leaves the row `running` with a finished task, and the cleanup tick
   removes it. Ready restores are removed after RESTORE_TTL_HOURS (extendable).
3. replace production (task `restore_replace`, maintenance tier): pre-restore
   backup (real run_backup, tags pre-restore + restore:<id>, must succeed) ->
   dump the scratch DB -> upload to the job's server -> rollback copy of
   production there -> import over the job's normal connection path ->
   verify again; an import failure or a key-table mismatch imports the
   rollback copy back (task status rolled_back).
4. undo (task `restore_undo`): scratch restore of the pre-restore snapshot +
   the same replace flow (with its own pre-restore backup).
"""

import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from datetime import timedelta

from . import pitr
from . import restore as r
from . import restore_scripts as rs
from . import security
from . import procs
from . import tasks

logger = logging.getLogger(__name__)


class RestoreError(Exception):
    def __init__(self, message, code='restore_error', status=400, extra=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}


def _m():
    from . import main
    return main


def _db():
    return _m().get_db()


def _naive(dt):
    dt = r.utc(dt)
    return dt.replace(tzinfo=None) if dt else None


def _json(text, default):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def create_row(row):
    now = _naive(r.utcnow())
    row = dict(row)
    row.setdefault('created_at', now)
    row['updated_at'] = now
    for key in ('plan', 'info', 'verify', 'pre_restore_snapshot'):
        if key in row and not isinstance(row[key], (str, type(None))):
            row[key] = json.dumps(row[key], default=str)
    cols = sorted(row)
    db = _db()
    db.execute(f"INSERT INTO restores ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
               tuple(row[c] for c in cols))
    db.commit()
    return get_row(row['id'])


def update_row(restore_id, **fields):
    fields['updated_at'] = _naive(r.utcnow())
    for key in ('plan', 'info', 'verify', 'pre_restore_snapshot'):
        if key in fields and not isinstance(fields[key], (str, type(None))):
            fields[key] = json.dumps(fields[key], default=str)
    cols = sorted(fields)
    db = _db()
    db.execute(f"UPDATE restores SET {', '.join(c + ' = %s' for c in cols)} WHERE id = %s",
               tuple(fields[c] for c in cols) + (restore_id,))
    db.commit()


def get_row(restore_id):
    row = _db().fetchone('SELECT * FROM restores WHERE id = %s', (restore_id,))
    return dict(row) if row else None


def list_rows(job_id=None, active=False, limit=50):
    where, params = [], []
    if job_id:
        where.append('job_id = %s')
        params.append(job_id)
    if active:
        where.append('status IN (' + ', '.join(['%s'] * len(r.LIVE_STATUSES)) + ')')
        params.extend(r.LIVE_STATUSES)
    sql = 'SELECT * FROM restores' + (' WHERE ' + ' AND '.join(where) if where else '')
    sql += ' ORDER BY created_at DESC LIMIT %s'
    params.append(max(1, min(int(limit or 50), 200)))
    return [dict(x) for x in _db().fetchall(sql, tuple(params))]


def _task(task_id):
    return tasks.serialize(tasks.get_task(task_id)) if task_id else None


def serialize(row):
    if not row:
        return None
    plan = _json(row.get('plan'), {})
    info = _json(row.get('info'), {})
    pre = _json(row.get('pre_restore_snapshot'), None)
    cfg = r.settings()
    pre_expires = None
    if row.get('pre_restore_at'):
        pre_expires = r.iso(r.utc(row['pre_restore_at']) + timedelta(days=cfg['pre_restore_keep_days']))
    engine = row.get('engine')
    container = row.get('container')
    hint = None
    if container and row.get('status') in r.LIVE_STATUSES and engine in r.DB_ENGINES:
        if engine == r.ENGINE_POSTGRES:
            hint = f"docker exec -it {container} psql -U {info.get('superuser') or 'postgres'}"
        else:
            hint = f"docker exec -it {container} {rs.mysql_flavor_bits(engine)['client_name']} -uroot"
    elif row.get('work_dir') and row.get('target_type') == 'scratch':
        hint = f"ls {row['work_dir']}   (on the backupx server)"
    now = r.utcnow()
    expired = bool(row.get('cleanup_at') and r.utc(row['cleanup_at']) < now)
    return {
        'id': row['id'], 'parent_id': row.get('parent_id'),
        'job_id': row['job_id'], 'job_name': row.get('job_name'),
        'engine': engine, 'point_type': row.get('point_type'), 'target_type': row.get('target_type'),
        'snapshot_id': row.get('snapshot_id'), 'target_time': r.iso(row.get('target_time')),
        'storage_id': row.get('storage_id'), 'status': row['status'],
        'server': row.get('server') or 'backupx', 'container': container, 'work_dir': row.get('work_dir'),
        'connect_hint': hint, 'plan': plan, 'info': info,
        'task': _task(row.get('task_id')), 'replace_task': _task(row.get('replace_task_id')),
        'undo_task': _task(row.get('undo_task_id')),
        'verify': _json(row.get('verify'), None),
        'pre_restore_snapshot': pre, 'pre_restore_expires_at': pre_expires,
        'cleanup_at': r.iso(row.get('cleanup_at')),
        'can_replace': (row['status'] == r.S_READY and engine in r.DB_ENGINES and not expired
                        and row.get('target_type') == 'scratch'),
        'can_undo': bool(row['status'] == r.S_REPLACED and pre and not row.get('pre_restore_forgotten_at')),
        'error': row.get('error'),
        'created_at': r.iso(row.get('created_at')), 'updated_at': r.iso(row.get('updated_at')),
        'created_by': row.get('created_by'),
    }


# ---------------------------------------------------------------------------
# Job context
# ---------------------------------------------------------------------------

def job_context(job_id):
    """(job with id, db_config or None, server or None, engine, pitr_state dict)."""
    m = _m()
    job = m.get_job(job_id)
    if not job:
        raise RestoreError('Job not found', 'not_found', 404)
    job = dict(job, id=job_id)
    db_config = m.get_db_config(job['database_config_id']) if job.get('database_config_id') else None
    server = m.get_server(job['server_id']) if job.get('server_id') else None
    state = {}
    if job.get('backup_type') == 'database':
        try:
            from . import pitr_service
            state = pitr_service.load_state(job_id)
        except Exception:
            state = {}
    if job.get('backup_type') == 'database' and not db_config:
        raise RestoreError('The job has no database configuration', 'not_restorable')
    engine = r.engine_for(job, db_config, state.get('server_version') or '')
    return job, db_config, server, engine, state


def _storage(job, storage_id):
    target = _m().resolve_job_storage(job, storage_id or None)
    if not target:
        raise RestoreError('Storage not found for this job', 'not_found', 404)
    return target


def _windows(job, engine, state):
    if not job.get('pitr_enabled') or engine == r.ENGINE_FILES:
        return []
    from . import pitr_service
    if engine == r.ENGINE_POSTGRES:
        return pitr_service._pg_windows(state)
    return pitr_service.compute_windows(job['id'], state)


def _pitr_method(engine):
    return 'walg' if engine == r.ENGINE_POSTGRES else 'binlog'


def verify_config(job, engine=None):
    data = _json(job.get('restore_verify'), {}) or {}
    return {'key_tables': list(data.get('key_tables') or []), 'check_sql': data.get('check_sql') or '',
            'health_url': data.get('health_url') or '',
            'drill_count_only_tables': list(data.get('drill_count_only_tables') or [])}


def set_verify_config(job_id, data):
    job, db_config, server, engine, state = job_context(job_id)
    data = dict(data or {})
    if 'drill_count_only_tables' not in data:  # older clients don't send it: keep the stored list
        data['drill_count_only_tables'] = verify_config(job).get('drill_count_only_tables') or []
    config, errors = r.normalize_verify_config(data, engine)
    if errors:
        raise RestoreError('; '.join(errors), 'invalid', 400)
    db = _db()
    db.execute('UPDATE jobs SET restore_verify = %s WHERE id = %s', (json.dumps(config), job_id))
    db.commit()
    return config


def restore_points(job_id, storage_id=None):
    m = _m()
    job, db_config, server, engine, state = job_context(job_id)
    targets = m.get_job_storage_targets(job, include_inactive=True)
    target = _storage(job, storage_id)
    snapshots, error = [], None
    problem = storage_precheck(target)
    if problem:
        # don't let restic retry a storage that is down for minutes while the wizard waits
        error = f"Storage {target.get('name')} is not reachable: {problem.rstrip('.')}"
    else:
        try:
            snapshots = m.get_snapshots(job, target)
        except Exception as e:
            error = m.sanitize_error_message(str(e))
    windows = _windows(job, engine, state)
    usable = r.restorable_windows(windows)
    ser = [{'from': r.iso(w.get('from')), 'to': r.iso(w.get('to')), 'anchored': bool(w.get('anchored')),
            'reason': w.get('reason') or ''} for w in windows]
    return {
        'job_id': job_id, 'job_name': job.get('name'), 'engine': engine,
        'backup_type': job.get('backup_type') or 'filesystem',
        'storages': [{'id': t['id'], 'name': t['name'], 'primary': i == 0} for i, t in enumerate(targets)],
        'storage_id': target['id'],
        'latest': r.latest_point(engine, snapshots, windows, job.get('pitr_enabled')),
        'pitr': {
            'enabled': bool(job.get('pitr_enabled')) and engine != r.ENGINE_FILES,
            'state': state.get('state') or ('pending' if job.get('pitr_enabled') else 'disabled'),
            'windows': ser, 'gaps': r.pitr_gaps(windows),
            'min': r.iso(min((r.utc(w['from']) for w in usable), default=None)),
            'max': r.iso(max((r.utc(w['to']) for w in usable), default=None)),
        },
        'snapshots': snapshots, 'snapshots_error': error,
        'verify_config': verify_config(job, engine),
    }


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def _free_bytes(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    return shutil.disk_usage(path).free


def _find_snapshot(snapshots, snapshot_id):
    for s in snapshots:
        if s.get('id') == snapshot_id or s.get('short_id') == snapshot_id or \
                (snapshot_id and len(snapshot_id) >= 8 and (s.get('id') or '').startswith(snapshot_id)):
            return s
    return None


def walg_env(target, prefix):
    m = _m()
    return {
        'WALG_S3_PREFIX': f"s3://{target['bucket']}/{prefix}{pitr.WAL_REPO_SUFFIX}",
        'AWS_ACCESS_KEY_ID': target.get('access_key') or '',
        'AWS_SECRET_ACCESS_KEY': target.get('secret_key') or '',
        'AWS_ENDPOINT': m.s3_endpoint_url(target['endpoint']),
        'AWS_S3_FORCE_PATH_STYLE': 'true',
        'AWS_REGION': target.get('region') or 'us-east-1',
    }


def walg_backups(job, target):
    """wal-g backup-list --detail in this container (empty list + error text when unavailable)."""
    binary = shutil.which('wal-g') or ('/usr/local/bin/wal-g' if os.path.exists('/usr/local/bin/wal-g') else None)
    if not binary:
        return [], 'wal-g is not installed in the backupx container'
    env = dict(os.environ, **walg_env(target, job.get('backup_prefix') or job['id']))
    try:
        proc = subprocess.run([binary, 'backup-list', '--json', '--detail'], capture_output=True, text=True,
                              env=env, timeout=120)
    except Exception as e:
        return [], str(e)
    if proc.returncode != 0:
        return [], (proc.stderr or '').strip()[-300:] or 'wal-g backup-list failed'
    try:
        return json.loads(proc.stdout or '[]') or [], None
    except ValueError:
        return [], 'unreadable wal-g output'


def _pick_base(backups, target_time):
    done = [b for b in backups if b.get('finish_time') and r.utc(b['finish_time']) <= r.utc(target_time)]
    return max(done, key=lambda b: r.utc(b['finish_time'])) if done else None


def derive_plan(data, cfg=None):
    """(plan, error). plan is deterministic for the same state; error = refusal text (disk...)."""
    cfg = cfg or r.settings()
    data = data or {}
    job_id = data.get('job_id')
    if not job_id:
        raise RestoreError('job_id is required', 'invalid')
    point_type = data.get('point_type') or r.POINT_LATEST
    if point_type not in r.POINT_TYPES:
        raise RestoreError('point_type must be latest, snapshot or time', 'invalid')
    target_type = data.get('target') or r.TARGET_SCRATCH
    if target_type not in r.DB_TARGETS:
        raise RestoreError('target must be scratch or download', 'invalid')
    job, db_config, server, engine, state = job_context(job_id)
    target = _storage(job, data.get('storage_id'))
    m = _m()
    # Seconds, not the ~15 min restic/wal-g spend retrying a storage that is down
    problem = storage_precheck(target)
    if problem:
        raise RestoreError(f"Storage {target.get('name')} is not reachable: {problem.rstrip('.')}. Pick another "
                           'storage or retry when it is back.', 'storage_unreachable')
    windows = _windows(job, engine, state)
    snapshots = []
    try:
        snapshots = m.get_snapshots(job, target)
    except Exception as e:
        if point_type == r.POINT_SNAPSHOT or not windows:
            raise RestoreError(f"Can't list snapshots on {target['name']}: {m.sanitize_error_message(str(e))}",
                               'not_restorable')

    method, snap, target_time = None, None, None
    if point_type == r.POINT_SNAPSHOT:
        snap = _find_snapshot(snapshots, data.get('snapshot_id') or '')
        if not snap and data.get('snapshot_id'):
            # The snapshot list is cached per gunicorn worker (60 s): a snapshot another worker just
            # made (the pre-restore backup an Undo restores) isn't in this worker's copy yet
            m.invalidate_snapshot_cache(job_id)
            try:
                snapshots = m.get_snapshots(job, target)
            except Exception as e:
                raise RestoreError(f"Can't list snapshots on {target['name']}: {m.sanitize_error_message(str(e))}",
                                   'not_restorable')
            snap = _find_snapshot(snapshots, data['snapshot_id'])
        if not snap:
            raise RestoreError('Snapshot not found on this storage', 'not_restorable')
        method = 'files' if engine == r.ENGINE_FILES else 'dump'
    elif point_type == r.POINT_LATEST:
        latest = r.latest_point(engine, snapshots, windows, job.get('pitr_enabled'))
        if not latest:
            raise RestoreError('Nothing to restore yet: no snapshot on this storage', 'not_restorable')
        if latest['point_type'] == r.POINT_TIME:
            method = _pitr_method(engine)
            target_time = r.utc(latest['time'])
            echoed = data.get('target_time')
            if echoed and r.time_in_windows(echoed, windows):
                target_time = r.utc(echoed).replace(microsecond=0)  # the time the user saw in the plan
        else:
            snap = _find_snapshot(snapshots, latest['snapshot_id'])
            method = 'files' if engine == r.ENGINE_FILES else 'dump'
    else:
        if engine == r.ENGINE_FILES or not job.get('pitr_enabled'):
            raise RestoreError('Exact-time restores need point-in-time recovery on this job', 'not_restorable')
        if not data.get('target_time'):
            raise RestoreError('target_time is required', 'invalid')
        try:
            target_time = r.utc(data['target_time']).replace(microsecond=0)
        except (TypeError, ValueError):
            raise RestoreError('target_time is not a valid time', 'invalid')
        if not r.time_in_windows(target_time, windows):
            raise RestoreError(f'{r.fmt_time(target_time)} is outside the restorable window(s)', 'not_restorable')
        method = _pitr_method(engine)

    if target_type == r.TARGET_DOWNLOAD and method != 'dump':
        raise RestoreError('Exact-time restores download as a restore script (POST /api/jobs/<id>/pitr/'
                           'restore-script); files download as a ZIP', 'use_script')

    plan = {
        'job_id': job_id, 'job_name': job.get('name'), 'engine': engine, 'method': method,
        'point_type': point_type, 'target': target_type, 'target_time': r.iso(target_time),
        'storage': {'id': target['id'], 'name': target['name']},
        'dump': None, 'binlogs': None, 'wal': None, 'image': None, 'warnings': [],
    }
    binlog_bytes = 0
    base_uncompressed = None
    if method in ('dump', 'files'):
        plan['dump'] = {'snapshot_id': snap['id'], 'time': snap.get('time'), 'size': r.snapshot_size(snap),
                        'path': r.dump_path(snap) if method == 'dump' else None,
                        'paths': snap.get('paths') or []}
        plan['target_time'] = r.iso(snap.get('time'))
        if method == 'dump':
            plan['image'] = r.scratch_image(engine, state.get('server_version') or '')
            if r.is_pre_restore(snap):
                plan['warnings'].append('This is a pre-restore snapshot (taken before a replace-production).')
    elif method == 'binlog':
        from . import pitr_service
        try:
            inputs = pitr_service.mysql_restore_inputs(job, target_time, target)
        except pitr.RestorePlanError as e:
            raise RestoreError(f'Not restorable: {e}', 'not_restorable')
        p = inputs['plan']
        anchor = p['anchor']
        dump_snap = _find_snapshot(snapshots, anchor['snapshots'].get(target['id'] or '', ''))
        files = sum(int(s['last_seq']) - int(s['first_seq']) + 1 for s in p['segments'])
        binlog_bytes = sum(int(s.get('size') or 0) for s in p['segments'])
        if p.get('partial'):
            files += 1
            binlog_bytes += int(p['partial'].get('size') or inputs.get('partial_size') or 0)
        plan['dump'] = {'snapshot_id': anchor['snapshots'].get(target['id'] or ''), 'time': r.iso(anchor['time']),
                        'size': r.snapshot_size(dump_snap), 'path': None, 'paths': []}
        plan['binlogs'] = {'files': files, 'segments': len(p['segments']), 'size': binlog_bytes,
                           'first': anchor['binlog_file'],
                           'last': (p['partial'] or (p['segments'][-1] if p['segments'] else {})).get('last_file')
                           or anchor['binlog_file'],
                           'partial': bool(p.get('partial')), 'start_pos': int(anchor['binlog_pos'])}
        plan['image'] = inputs['server_image']
        plan['binlog_image'] = inputs['binlog_image']
        if dump_snap is None:
            plan['warnings'].append('The dump snapshot is not in the snapshot list; its size is unknown.')
    elif method == 'walg':
        backups, err = walg_backups(job, target)
        base = _pick_base(backups, target_time)
        if base is None and not err:
            raise RestoreError(f'No WAL-G base backup finished before {r.fmt_time(target_time)}', 'not_restorable')
        major = r.pg_major((state.get('server_version') or '').replace('PostgreSQL', ''))
        plan['wal'] = {'base_backup': base.get('backup_name') if base else None,
                       'base_time': r.iso(base.get('finish_time')) if base else None,
                       'base_finish_lsn': r.pg_lsn(base.get('finish_lsn')) if base else None,
                       'uncompressed_size': base.get('uncompressed_size') if base else None,
                       'compressed_size': base.get('compressed_size') if base else None,
                       'pg_major': major}
        base_uncompressed = (base or {}).get('uncompressed_size')
        plan['image'] = r.walg_scratch_image(major)
        if err:
            plan['warnings'].append(f"Couldn't list base backups here ({err}); the newest one before the "
                                    'target is picked when the restore runs.')

    dump_size = (plan['dump'] or {}).get('size') or 0
    wal_dl = (plan['wal'] or {}).get('compressed_size') or 0
    plan['download_bytes'] = int(dump_size + binlog_bytes + wal_dl)
    plan['estimated_restored_bytes'] = r.estimate_restored_bytes(method, dump_size, binlog_bytes,
                                                                base_uncompressed, cfg)
    if method == 'files':
        plan['where'] = (f"backupx server: {cfg['host_files_dir']}/{job_id}-<time> "
                         f"(removed after {cfg['ttl_hours']} h)")
    elif target_type == r.TARGET_DOWNLOAD:
        plan['where'] = 'download: the dump file from the snapshot'
    else:
        plan['where'] = (f"backupx server: scratch container (no published ports, removed after "
                         f"{cfg['ttl_hours']} h unless kept longer)")
    if target_type == r.TARGET_SCRATCH and engine in r.DB_ENGINES:
        # Off unless asked for: the default checks read the restored copy only, never production
        plan['compare_production'] = wants_production_compare(data)
    plan['steps'] = _scratch_steps(plan)
    plan['summary'] = r.plan_summary(plan)
    if engine in r.DB_ENGINES and not shutil.which('docker') and target_type == r.TARGET_SCRATCH:
        plan['warnings'].append('The docker CLI is not available in the backupx container: scratch restores '
                                'need the P2 image and /var/run/docker.sock (see DEPLOYMENT.md).')
    error = None
    if target_type == r.TARGET_SCRATCH:
        try:
            free = _free_bytes(cfg['work_dir'])
        except OSError:
            free = 0
        plan['disk'] = r.disk_check(cfg['work_dir'], free, plan['estimated_restored_bytes'], cfg)
        if not plan['disk']['ok']:
            error = r.disk_error(plan['disk'], plan['estimated_restored_bytes'], cfg)
    else:
        plan['disk'] = None
    return plan, error


def _scratch_steps(plan):
    m = plan['method']
    if plan['target'] == r.TARGET_DOWNLOAD:
        return [f"Fetch {plan['dump']['path'] or 'the dump'} from snapshot {plan['dump']['snapshot_id'][:8]}",
                'Offer it as a download (24 h)']
    if m == 'files':
        return ['Restore the snapshot into a work directory in the backupx container',
                'Copy it to the extract directory on the backupx server']
    check = ('Compare row counts with production' if plan.get('compare_production')
             else 'Count rows in the restored copy (production not queried)')
    if m == 'dump':
        return ['Fetch the dump from the snapshot', 'Start a scratch database container (no published ports)',
                'Load the dump', check]
    if m == 'binlog':
        return ['Fetch the anchored dump and the binlog segments', 'Decode the binlogs up to the target second',
                'Start a scratch database container (no published ports)', 'Load the dump and replay the binlogs',
                check]
    return ['Fetch the WAL-G base backup', 'Start a scratch PostgreSQL container with recovery to the target time',
            'Replay WAL and promote', check]


def wants_production_compare(data):
    """A new scratch restore compares with production only when the request says so explicitly."""
    return (data or {}).get('compare_production') is True


def present(plan):
    return {'plan': plan, 'plan_hash': security.plan_hash(r.hashed_plan(plan))}


# ---------------------------------------------------------------------------
# Starting restores (routes call these)
# ---------------------------------------------------------------------------

def _active_task_for_job(job_id):
    row = _db().fetchone(
        "SELECT id FROM tasks WHERE resource_type = 'job' AND resource_id = %s AND status IN ('queued', 'running')"
        " AND kind IN ('restore_scratch', 'restore_replace', 'restore_undo', 'restore_files_paths')", (job_id,))
    return row['id'] if row else None


def start_restore(data, user=None, ip=None):
    """Create the row + task for a confirmed plan. Returns (restore row, task row)."""
    plan, error = derive_plan(data)
    submitted = (data or {}).get('plan_hash')
    if not submitted:
        raise RestoreError('Review the plan first (plan_hash missing)', 'plan_hash_missing', 400)
    current = security.plan_hash(r.hashed_plan(plan))
    if submitted != current:
        raise RestoreError('The plan changed since you reviewed it; review it again', 'plan_changed', 409,
                           dict(present(plan), ok=error is None, **({'plan_error': error} if error else {})))
    if error:
        raise RestoreError(error, 'insufficient_disk', 400, present(plan))
    if plan['target'] == r.TARGET_SCRATCH and _active_task_for_job(plan['job_id']):
        raise RestoreError('A restore of this job is already running', 'busy', 409)
    restore_id = tasks.new_id()
    kind = 'restore_download' if plan['target'] == r.TARGET_DOWNLOAD else 'restore_scratch'
    if plan['method'] == 'files':
        kind = 'restore_files_paths'
    row = create_row({
        'id': restore_id, 'job_id': plan['job_id'], 'job_name': plan['job_name'], 'engine': plan['engine'],
        'point_type': plan['point_type'], 'snapshot_id': (plan.get('dump') or {}).get('snapshot_id'),
        'target_time': _naive(plan.get('target_time')), 'storage_id': plan['storage']['id'],
        'target_type': 'scratch' if plan['target'] == r.TARGET_SCRATCH else 'download', 'server': 'backupx',
        'container': r.container_name(restore_id) if plan['engine'] in r.DB_ENGINES and kind == 'restore_scratch'
        else None,
        'plan': plan, 'info': ({'files_target': 'scratch'} if kind == 'restore_files_paths' else
                               {'compare_production': wants_production_compare(data)}),
        'status': r.S_RUNNING, 'created_by': user,
    })
    if kind == 'restore_files_paths':
        plan = dict(plan, paths=[], files_target='scratch')
        update_row(restore_id, plan=plan)
    title = {'restore_scratch': f"Scratch restore of {plan['job_name']}",
             'restore_download': f"Prepare dump of {plan['job_name']}",
             'restore_files_paths': f"Extract {plan['job_name']} on the backupx server"}[kind]
    task, _ = tasks.enqueue(kind, {'restore_id': restore_id, 'job_id': plan['job_id']}, title=title,
                            resource_type='job', resource_id=plan['job_id'], created_by=user, ip=ip,
                            idempotency_key=f'restore:{restore_id}')
    update_row(restore_id, task_id=task['id'])
    return get_row(restore_id), task


def extend(restore_id, hours=24):
    row = get_row(restore_id)
    if not row:
        raise RestoreError('Restore not found', 'not_found', 404)
    if row['status'] not in r.LIVE_STATUSES or not row.get('cleanup_at'):
        raise RestoreError('Nothing to keep: this restore has no scratch copy', 'not_ready', 409)
    until = r.extend_until(row['cleanup_at'], hours=hours)
    update_row(restore_id, cleanup_at=_naive(until))
    return get_row(restore_id)


# ---------------------------------------------------------------------------
# Local execution (the backupx container)
# ---------------------------------------------------------------------------

def _require_docker():
    if not shutil.which('docker'):
        raise tasks.TaskError('The docker CLI is not available in the backupx container. Scratch restores need '
                              'the P2 image (docker CLI) and /var/run/docker.sock with the docker group '
                              '(DOCKER_GID) - see DEPLOYMENT.md.')


def _popen_local(script, env):
    proc = subprocess.Popen(['bash', '-s'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)

    def feed():
        try:
            proc.stdin.write(script)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
    threading.Thread(target=feed, daemon=True).start()
    return proc


def run_local(ctx, script, env=None, timeout=None, on_line=None):
    """Run a script here (bash -s), log it into the task, return the exit code."""
    proc = _popen_local(script, env if env is not None else os.environ.copy())
    return ctx.stream(proc, timeout=timeout or r.settings()['timeout'], on_line=on_line)


def capture_local(script, env=None, timeout=600):
    """Run a script here and return CompletedProcess (not logged; for query output)."""
    return subprocess.run(['bash', '-s'], input=script, capture_output=True, text=True,
                          env=env if env is not None else os.environ.copy(), timeout=timeout)


def stream_cmd(ctx, cmd, env, timeout=None, stdout=None):
    proc = subprocess.Popen(cmd, stdout=stdout if stdout is not None else subprocess.PIPE,
                            stderr=subprocess.PIPE, text=stdout is None, env=env)
    return ctx.stream(proc, timeout=timeout or r.settings()['timeout'])


def _restic_env(job, target):
    return _m().job_restic_env(job, target)


def _insecure(target):
    return ['--insecure-tls'] if target.get('skip_ssl_verify') else []


def remove_scratch(row, log=None):
    """Remove the restore's container(s) and work/extract directories. Never raises."""
    cfg = r.settings()
    out = []
    try:
        if shutil.which('docker'):
            res = capture_local(rs.remove_containers_script(row['id']), timeout=180)
            out.append((res.stdout + res.stderr).strip())
            info = _json(row.get('info'), {})
            if info.get('host_dir'):
                res = capture_local(rs.remove_host_dir_script(info['host_dir'], cfg['helper_image']), timeout=180)
                out.append((res.stdout + res.stderr).strip() or f"Removed {info['host_dir']}")
    except Exception as e:
        out.append(f'container cleanup failed: {e}')
    work = os.path.join(cfg['work_dir'], row['id'])
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
        out.append(f'Removed {work}')
    text = '\n'.join(x for x in out if x)
    if log and text:
        log(text)
    return text


# ---------------------------------------------------------------------------
# Scratch restore
# ---------------------------------------------------------------------------

def _row_or_fail(ctx):
    row = get_row(ctx.params.get('restore_id'))
    if not row:
        raise tasks.TaskError('Restore not found')
    return row


def _read_header(path, limit=65536):
    try:
        with gzip.open(path, 'rt', errors='replace') as f:
            return f.read(limit)
    except OSError:
        return ''


def _find_file(root, suffix='.sql.gz'):
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.endswith(suffix):
                return os.path.join(base, name)
    return None


def _restic_restore(ctx, job, target, snapshot_id, dest, includes=()):
    os.makedirs(dest, mode=0o700, exist_ok=True)
    cmd = ['restic', 'restore', snapshot_id, '--target', dest, *_insecure(target)]
    for inc in includes:
        cmd += ['--include', inc]
    rc = stream_cmd(ctx, cmd, _restic_env(job, target))
    if rc != 0:
        raise tasks.TaskError(f'restic restore failed (exit code {rc}); see the log')


def _scratch_dump(ctx, row, plan, job, db_config, target, work, engine):
    ctx.step(1, 4, f"Fetching snapshot {plan['dump']['snapshot_id'][:8]}")
    _restic_restore(ctx, job, target, plan['dump']['snapshot_id'], os.path.join(work, 'dump'))
    dump = _find_file(os.path.join(work, 'dump'))
    if not dump:
        raise tasks.TaskError('No .sql.gz dump in the snapshot')
    header = _read_header(dump)
    version = r.server_version_from_dump(header)
    if engine == r.ENGINE_MYSQL and 'mariadb' in (version or '').lower():
        engine = r.ENGINE_MARIADB  # configured as mysql, the dump says MariaDB
    image = plan.get('image') or r.scratch_image(engine, version)
    if not image:
        image = 'mysql:8.4' if engine == r.ENGINE_MYSQL else ('mariadb:11.4' if engine == r.ENGINE_MARIADB else None)
    if not image:
        raise tasks.TaskError("Can't tell the PostgreSQL version from the dump header")
    ctx.log(f'Dump: {os.path.basename(dump)}; server version {version or "unknown"}; scratch image {image}',
            stream='system')
    info = {'server_version': version, 'image': image, 'engine': engine}
    d_args = rs.docker_args(row['id'], job['id'])
    if engine == r.ENGINE_POSTGRES:
        dbs = r.configured_databases(db_config)
        mode = 'cluster' if not dbs else ('single' if len(dbs) == 1 else 'multi')
        script = rs.pg_scratch_load(row['container'], image, dump, mode, dbs, work, d_args, n=2, m=4)
        info['superuser'] = 'postgres'
    else:
        script = rs.mysql_scratch_load(row['container'], image, engine, dump, d_args, n=2, m=4)
    rc = run_local(ctx, script)
    if rc != 0:
        raise tasks.TaskError(f'Loading the dump into the scratch server failed (exit code {rc}); see the log')
    return info


def _scratch_binlog(ctx, row, plan, job, target, work, engine):
    from . import pitr_scripts, pitr_service
    try:
        inputs = pitr_service.mysql_restore_inputs(job, r.utc(plan['target_time']), target)
    except pitr.RestorePlanError as e:
        raise tasks.TaskError(f'Not restorable any more: {e}')
    script = pitr_scripts.mysql_restore_script(job, inputs['plan'], r.utc(plan['target_time']), target,
                                               inputs['main_repo'], inputs['binlog_repo'], inputs['server_image'],
                                               inputs['binlog_image'], inputs['flavor'])
    env = _restic_env(job, target)
    env.pop('RESTIC_REPOSITORY', None)
    env.update({'NAME': row['container'], 'WORK': work, 'BACKUPX_STEPS': '1',
                'DOCKER_ARGS': rs.docker_args(row['id'], job['id'])})
    os.makedirs(work, mode=0o700, exist_ok=True)
    seen = {}

    def on_line(_stream, line):
        last = r.parse_last_event_marker(line)
        if last is not None:
            seen['last_event_at'] = last

    rc = run_local(ctx, script, env=env, on_line=on_line)
    if rc != 0:
        raise tasks.TaskError(f'The point-in-time restore failed (exit code {rc}); see the log')
    return {'server_version': inputs['server_version'], 'image': inputs['server_image'],
            'binlog_image': inputs['binlog_image'],
            'last_event_at': r.iso(seen['last_event_at']) if seen.get('last_event_at') else None}


def _scratch_walg(ctx, row, plan, job, db_config, target, work):
    cfg = r.settings()
    binary = shutil.which('wal-g') or '/usr/local/bin/wal-g'
    if not os.path.exists(binary):
        raise tasks.TaskError('wal-g is not installed in the backupx container (P2 image)')
    target_time = r.utc(plan['target_time'])
    env = dict(os.environ, **walg_env(target, job.get('backup_prefix') or job['id']))
    ctx.add_secret(env['AWS_SECRET_ACCESS_KEY'], env['AWS_ACCESS_KEY_ID'])
    ctx.step(1, 5, 'Choosing the base backup')
    backups, err = walg_backups(job, target)
    immediate = plan.get('recovery_target') == 'immediate'
    target_lsn = plan.get('recovery_target_lsn') or ''
    wanted = (plan.get('wal') or {}).get('base_backup') if (immediate or target_lsn) else None
    base = next((b for b in backups if b.get('backup_name') == wanted), None) if wanted else \
        _pick_base(backups, target_time)
    if not base:
        raise tasks.TaskError(err or f'No base backup finished before {r.fmt_time(target_time)}')
    pgdata = os.path.join(work, 'pgdata')
    os.makedirs(pgdata, mode=0o700, exist_ok=True)
    ctx.step(2, 5, f"Fetching base backup {base['backup_name']}")
    rc = stream_cmd(ctx, [binary, 'backup-fetch', pgdata, base['backup_name']], env)
    if rc != 0:
        raise tasks.TaskError(f'wal-g backup-fetch failed (exit code {rc})')
    try:
        major = open(os.path.join(pgdata, 'PG_VERSION')).read().strip()
    except OSError:
        raise tasks.TaskError('The base backup has no PG_VERSION file')
    helper = os.path.join(pgdata, 'backupx-walg')
    os.makedirs(helper, mode=0o700, exist_ok=True)
    shutil.copy2(binary, os.path.join(helper, 'wal-g'))
    os.chmod(os.path.join(helper, 'wal-g'), 0o755)
    with open(os.path.join(helper, 'fetch.sh'), 'w') as f:
        f.write(rs.WALG_FETCH_SH)
    os.chmod(os.path.join(helper, 'fetch.sh'), 0o755)
    fd = os.open(os.path.join(helper, 'env'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        for k, v in walg_env(target, job.get('backup_prefix') or job['id']).items():
            f.write(f'{k}={rs.q(v)}\n')
    t = target_time.strftime('%Y-%m-%d %H:%M:%S+00')
    restore_cmd = "restore_command = '/var/lib/postgresql/data/backupx-walg/fetch.sh \"%f\" \"%p\"'\n"
    # immediate: stop as soon as the base backup is consistent (the base alone, for restore drills);
    # an LSN: replay archived WAL up to exactly that position (the drill's "base backup's own point")
    if immediate:
        target_line = "recovery_target = 'immediate'\n"
    elif target_lsn and re.match(r'^[0-9A-F]{1,8}/[0-9A-F]{1,8}$', target_lsn):
        target_line = f"recovery_target_lsn = '{target_lsn}'\nrecovery_target_inclusive = true\n"
    else:
        target_line = f"recovery_target_time = '{t}'\n"
    settings = restore_cmd + target_line + "recovery_target_action = 'promote'\n"
    try:
        is_old = int(major.split('.')[0]) < 12
    except ValueError:
        is_old = True
    if is_old:
        with open(os.path.join(pgdata, 'recovery.conf'), 'w') as f:
            f.write(settings)
    else:
        with open(os.path.join(pgdata, 'postgresql.auto.conf'), 'a') as f:
            f.write('\n' + settings)
        open(os.path.join(pgdata, 'recovery.signal'), 'w').close()
    with open(os.path.join(pgdata, 'postgresql.auto.conf'), 'a') as f:
        f.write("\n# backupx scratch restore\narchive_mode = 'off'\n")
    if not os.path.exists(os.path.join(pgdata, 'postgresql.conf')):
        with open(os.path.join(pgdata, 'postgresql.conf'), 'w') as f:
            f.write("listen_addresses = 'localhost'\n")
    if os.path.exists(os.path.join(pgdata, 'pg_hba.conf')):
        shutil.move(os.path.join(pgdata, 'pg_hba.conf'), os.path.join(pgdata, 'pg_hba.conf.production'))
    with open(os.path.join(pgdata, 'pg_hba.conf'), 'w') as f:
        f.write('local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n')
    if not os.path.exists(os.path.join(pgdata, 'pg_ident.conf')):
        open(os.path.join(pgdata, 'pg_ident.conf'), 'w').close()
    try:
        os.unlink(os.path.join(pgdata, 'postmaster.pid'))
    except OSError:
        pass
    image = plan.get('image') or r.walg_scratch_image(major)
    d_create = rs.docker_args(row['id'], job['id'], network=cfg['docker_network'] or 'bridge')
    script = rs.pg_walg_start(row['container'], d_create, 3, 5)
    env2 = dict(os.environ, SERVER_IMAGE=image, PGDATA_SRC=pgdata, WAIT_TRIES=str(max(60, cfg['timeout'] // 3)),
                ALT_SU=(db_config or {}).get('username') or '')
    found = {}

    def on_line(_stream, line):
        if line.startswith('BACKUPX-SUPERUSER '):
            found['su'] = line.split(' ', 1)[1].strip()
    rc = run_local(ctx, script, env=env2, on_line=on_line)
    if rc != 0:
        raise tasks.TaskError(f'Recovery in the scratch container failed (exit code {rc}); see the log')
    return {'server_version': f'PostgreSQL {major}', 'image': image, 'superuser': found.get('su') or 'postgres',
            'base_backup': base['backup_name']}


def _scratch_files(ctx, row, plan, job, target, work):
    cfg = r.settings()
    paths = plan.get('paths') or []
    ctx.step(1, 2, f"Restoring snapshot {plan['dump']['snapshot_id'][:8]}")
    _restic_restore(ctx, job, target, plan['dump']['snapshot_id'], os.path.join(work, 'files'), paths)
    host_dir = r.default_alternate_path(job['id'], root=cfg['host_files_dir'])
    ctx.step(2, 2, f'Copying to {host_dir} on the backupx server')
    rc = run_local(ctx, rs.files_to_host_script(os.path.join(work, 'files'), host_dir, cfg['helper_image']))
    if rc != 0:
        raise tasks.TaskError(f'Copying to the backupx server failed (exit code {rc})')
    return {'host_dir': host_dir}


def compares_production(row):
    """Whether a scratch restore's checks also read production row counts (only when asked for; per restore)."""
    return _json(row.get('info'), {}).get('compare_production') is True


def storage_precheck(target, timeout=8):
    """None if the storage answers, else why not. Fast (seconds): restic and wal-g retry an
    unreachable or failing storage for about 15 minutes before giving up."""
    import ssl
    import urllib.request
    m = _m()
    kind, message = m.probe_s3_endpoint(target.get('endpoint') or '', target.get('skip_ssl_verify'), timeout)
    if kind != 'ok':
        return message
    url = f"{m.s3_endpoint_url(target['endpoint']).rstrip('/')}/{target.get('bucket') or ''}"
    ctx = None
    if url.startswith('https://') and target.get('skip_ssl_verify'):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        # Unauthenticated HEAD of the bucket: 403/404/200 = the storage answers; 5xx = it doesn't
        urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=timeout, context=ctx)
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            return f"{target.get('name')} answers HTTP {e.code} ({e.reason})"
    except Exception as e:
        return f"{target.get('name')} did not answer: {e}"
    return None


def do_scratch(ctx, row, verify_production=True):
    """Run the scratch restore of `row` (status running -> ready). Cleans up on failure; raises.

    verify_production=False (restore drills, or the restore's compare_production=false) checks the copy
    alone: production is never queried.
    """
    cfg = r.settings()
    plan = _json(row['plan'], {})
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    target = _storage(job, row.get('storage_id'))
    ctx.add_secret(*_m()._job_secret_values(job, [target]))
    work = os.path.join(cfg['work_dir'], row['id'])
    method = plan['method']
    _require_docker()
    try:
        problem = storage_precheck(target)
        if problem:
            raise tasks.TaskError(f"Storage {target.get('name')} is not reachable, nothing was restored: "
                                  f"{problem.rstrip('.')}. Pick another storage or retry when it is back.")
        with ctx.repo_locks([_m().job_repo_url(job, target)]):
            if method == 'dump':
                info = _scratch_dump(ctx, row, plan, job, db_config, target, work, engine)
            elif method == 'binlog':
                info = _scratch_binlog(ctx, row, plan, job, target, work, engine)
            elif method == 'walg':
                info = _scratch_walg(ctx, row, plan, job, db_config, target, work)
            elif method == 'files':
                info = _scratch_files(ctx, row, plan, job, target, work)
            else:
                raise tasks.TaskError(f'unknown restore method {method}')
        if info.get('engine') and info['engine'] != engine:
            engine = info['engine']
            update_row(row['id'], engine=engine)
        update_row(row['id'], info=dict(_json(row.get('info'), {}), **info),
                   work_dir=info.get('host_dir'))
        row = get_row(row['id'])
        if engine in r.DB_ENGINES:
            with_prod = verify_production and compares_production(row)
            ctx.log('BACKUPX-STEP 4/4 ' + ('Comparing row counts with production' if with_prod
                                           else 'Counting rows in the copy (production not queried)'),
                    stream='system')
            try:
                verify = run_verify(row, phase='scratch', production=with_prod)
                update_row(row['id'], verify=verify)
                ctx.log(_verify_line(verify), stream='system')
            except Exception as e:
                ctx.log(f'Verification failed (the restore itself is fine): {e}', stream='system')
        update_row(row['id'], status=r.S_READY, cleanup_at=_naive(r.cleanup_at(cfg=cfg)), error=None)
    except BaseException as e:
        remove_scratch(row, log=lambda t: ctx.log(t, stream='system'))
        status = r.S_CANCELLED if isinstance(e, tasks.TaskCancelled) else r.S_FAILED
        update_row(row['id'], status=status, error=str(e)[:1000] or type(e).__name__)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return get_row(row['id'])


def _verify_line(verify):
    s = verify.get('summary') or {}
    if verify.get('production') is False:
        return f"Counted rows in {s.get('tables', 0)} tables of the restored copy (production not queried)"
    text = f"Compared {s.get('tables', 0)} tables"
    if s.get('key_tables'):
        text += f", {s['key_tables']} key tables ({s.get('key_mismatches', 0)} differ)"
    else:
        text += ', no key tables configured (counts for information only)'
    if verify.get('prod_error'):
        text += f"; production not queried: {verify['prod_error']}"
    return text


@tasks.register('restore_scratch', title='Scratch restore', tier=tasks.TIER_SAFE, cancellable=True)
def _task_scratch(ctx):
    row = _row_or_fail(ctx)
    m = _m()
    m._audit('RESTORE_SCRATCH', 'job', row['job_id'], row.get('job_name'),
             {'restore_id': row['id'], 'phase': 'started', 'task': ctx.id}, user=ctx.created_by, ip=ctx.ip)
    try:
        row = do_scratch(ctx, row)
    except BaseException as e:
        m._audit('RESTORE_SCRATCH', 'job', row['job_id'], row.get('job_name'),
                 {'restore_id': row['id'], 'phase': 'finished', 'task': ctx.id}, status='failure',
                 error=str(e)[:300] or type(e).__name__, user=ctx.created_by, ip=ctx.ip)
        raise
    m._audit('RESTORE_SCRATCH', 'job', row['job_id'], row.get('job_name'),
             {'restore_id': row['id'], 'phase': 'finished', 'task': ctx.id}, user=ctx.created_by, ip=ctx.ip)
    where = row.get('container') or row.get('work_dir')
    return {'message': f'Restored into {where}', 'restore_id': row['id']}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _client_image(job, db_config):
    if (db_config.get('docker_container') or '').strip():
        return ''
    return pitr.effective_client_image(job) if job.get('pitr_enabled') else ''


def _parse_verify(out, engine):
    est, exact, check, errors = {}, {}, None, []
    dbs = []
    for label, (rc, lines) in out.items():
        if label == 'est' or label.startswith('est:'):
            if rc == 0:
                est.update(r.parse_counts(lines))
            else:
                errors.append(' '.join(lines[-2:]))
        elif label.startswith('exact:'):
            if rc == 0 and lines:
                try:
                    exact[label[6:]] = int(lines[-1].strip())
                except ValueError:
                    pass
        elif label == 'check':
            check = (rc, lines)
        elif label == 'dbs' and rc == 0:
            dbs = [x.strip() for x in lines if x.strip()]
    return est, exact, check, errors, dbs


class _NotQueried(Exception):
    pass


def prod_sql(job, db_config, server, body, timeout=600):
    m = _m()
    if not server:
        raise RestoreError('The job has no server to reach the production database from', 'no_server')
    engine = r.engine_for(job, db_config)
    script = rs.PREAMBLE + rs.prod_runner(db_config, engine, _client_image(job, db_config)) + body
    return m._ssh_run(m._build_ssh_cmd_for_server(server, timeout=30), script, timeout=timeout)


def run_verify(row, phase='scratch', production=True):
    """Compare the scratch copy with production. Returns the verify dict (never raises on prod errors).

    production=False: the copy's counts only (prod_error says production wasn't queried).
    """
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    info = _json(row.get('info'), {})
    cfg = verify_config(job, engine)
    key_tables, _bad = r.normalize_key_tables(cfg['key_tables'], engine)
    databases = r.configured_databases(db_config)
    exact_max = r.settings()['verify_exact_max_rows']
    # The copy may refresh its statistics (ANALYZE); production only ever gets reads
    body = rs.verify_body(engine, databases, key_tables, cfg['check_sql'], analyze=True,
                          exact_max_rows=exact_max)
    prod_body = rs.verify_body(engine, databases, key_tables, cfg['check_sql'], analyze=False,
                               exact_max_rows=exact_max)
    local = capture_local(rs.PREAMBLE + rs.scratch_runner(engine, row['container'], info.get('superuser') or 'postgres')
                          + body, timeout=900)
    r_est, r_exact, r_check, r_errors, r_dbs = _parse_verify(r.parse_marked_output(local.stdout), engine)
    if r_dbs and not info.get('databases'):
        info['databases'] = [d for d in r_dbs if d not in r.MYSQL_SYSTEM_SCHEMAS]
        update_row(row['id'], info=info)
    p_est, p_exact, p_check, prod_error = {}, {}, None, None
    try:
        if not production:
            raise _NotQueried()
        res = prod_sql(job, db_config, server, prod_body)
        p_est, p_exact, p_check, p_errors, _ = _parse_verify(r.parse_marked_output(res.stdout), engine)
        if res.returncode != 0 and not p_est:
            prod_error = _m().sanitize_error_message((res.stderr or res.stdout or '').strip()[-300:] or
                                                     f'exit {res.returncode}')
        elif p_errors and not p_est:
            prod_error = _m().sanitize_error_message(p_errors[0])
    except _NotQueried:
        prod_error = 'not compared: production is not queried for this restore'
    except Exception as e:
        prod_error = _m().sanitize_error_message(str(e))
    secrets_ = _m()._job_secret_values(job)
    if prod_error:
        prod_error = tasks.redact_text(prod_error, secrets_)
    rows = r.compare(p_est, r_est, key_tables, p_exact, r_exact)
    check = None
    if cfg['check_sql']:
        def text(c):
            return None if c is None else tasks.redact_text('\n'.join(c[1][:50]), secrets_)
        check = {'prod': text(p_check), 'restored': text(r_check),
                 'match': (p_check is not None and r_check is not None and p_check[0] == 0 and r_check[0] == 0
                           and p_check[1] == r_check[1]) if (p_check and r_check) else None,
                 'error': None if (r_check and r_check[0] == 0) else 'the check failed on the restored copy'}
    health_url = None
    if phase == 'after_replace' and cfg['health_url']:
        health_url = check_health_url(cfg['health_url'])
    return {
        'checked_at': r.iso(r.utcnow()), 'phase': phase, 'production': bool(production), 'tables': rows,
        'prod_error': prod_error,
        'restored_error': (r_errors[0] if r_errors and not r_est else None),
        'check_sql': check, 'health_url': health_url,
        'summary': {'tables': len(rows), 'key_tables': len(key_tables),
                    'key_mismatches': len([x for x in rows if x['key'] and x['match'] is False]),
                    'exact_tables': len([x for x in rows if x.get('restored_exact') is not None]),
                    'exact_max_rows': exact_max if engine != r.ENGINE_POSTGRES else None},
    }


def check_health_url(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='GET'), timeout=15) as resp:
            return {'url': url, 'status': resp.status, 'ok': 200 <= resp.status < 300, 'error': None}
    except Exception as e:
        status = getattr(e, 'code', None)
        return {'url': url, 'status': status, 'ok': False, 'error': str(e)[:200]}


@tasks.register('restore_verify', title='Re-check a restore', tier=tasks.TIER_SAFE, cancellable=False)
def _task_verify(ctx):
    row = _row_or_fail(ctx)
    if row['status'] not in r.LIVE_STATUSES or not row.get('container'):
        raise tasks.TaskError('This restore has no scratch database any more')
    job, *_rest = job_context(row['job_id'])
    ctx.add_secret(*_m()._job_secret_values(job))
    phase = 'after_replace' if row['status'] == r.S_REPLACED else 'scratch'
    production = phase == 'after_replace' or compares_production(row)
    ctx.step(1, 1, 'Comparing row counts with production' if production else 'Counting rows in the restored copy')
    verify = run_verify(row, phase=phase, production=production)
    update_row(row['id'], verify=verify)
    return {'message': _verify_line(verify), 'restore_id': row['id']}


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@tasks.register('restore_download', title='Prepare dump download', tier=tasks.TIER_SAFE, cancellable=True)
def _task_download(ctx):
    row = _row_or_fail(ctx)
    plan = _json(row['plan'], {})
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    target = _storage(job, row.get('storage_id'))
    ctx.add_secret(*_m()._job_secret_values(job, [target]))
    path = plan['dump'].get('path')
    if not path:
        raise tasks.TaskError('The snapshot holds no dump file')
    name = f'{ctx.id}.sql.gz'
    out = os.path.join(tasks.files_dir(), name)
    ctx.step(1, 1, f"Fetching {os.path.basename(path)}")
    try:
        with ctx.repo_locks([_m().job_repo_url(job, target)]), open(out, 'wb') as f:
            rc = stream_cmd(ctx, ['restic', 'dump', plan['dump']['snapshot_id'], path, *_insecure(target)],
                            _restic_env(job, target), stdout=f)
        if rc != 0:
            raise tasks.TaskError(f'restic dump failed (exit code {rc})')
    except BaseException as e:
        try:
            os.unlink(out)
        except OSError:
            pass
        update_row(row['id'], status=r.S_FAILED if not isinstance(e, tasks.TaskCancelled) else r.S_CANCELLED,
                   error=str(e)[:500])
        raise
    update_row(row['id'], status=r.S_DONE)
    size = os.path.getsize(out)
    stamp = r.utc(plan['dump']['time']).strftime('%Y%m%dT%H%M%SZ') if plan['dump'].get('time') else 'dump'
    return {'file': name, 'filename': f"{job['id']}-{stamp}.sql.gz", 'mimetype': 'application/gzip',
            'size': size, 'download_url': f'/api/tasks/{ctx.id}/download', 'expires_in_hours': 24,
            'message': f'Dump ready ({r.fmt_bytes(size)})'}


def _restored_databases(row, db_config):
    dbs = r.configured_databases(db_config)
    if dbs:
        return dbs
    info = _json(row.get('info'), {})
    if info.get('databases'):
        return list(info['databases'])
    engine = row.get('engine')
    sql = ("SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres' "
           "ORDER BY 1" if engine == r.ENGINE_POSTGRES else
           "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ("
           + ', '.join(f"'{s}'" for s in r.MYSQL_SYSTEM_SCHEMAS) + ") ORDER BY 1")
    script = (rs.PREAMBLE + rs.scratch_runner(engine, row['container'], info.get('superuser') or 'postgres')
              + rs.RUN_SQL + f'run_sql dbs "" {rs.q(sql)}\n')
    res = capture_local(script, timeout=120)
    out = r.parse_marked_output(res.stdout).get('dbs')
    if not out or out[0] != 0:
        raise RestoreError("Can't list the restored databases in the scratch container", 'not_ready', 409)
    dbs = [x.strip() for x in out[1] if x.strip()]
    info['databases'] = dbs
    update_row(row['id'], info=info)
    return dbs


@tasks.register('restore_dump', title='Dump a scratch restore', tier=tasks.TIER_SAFE, cancellable=True)
def _task_dump(ctx):
    row = _row_or_fail(ctx)
    if row['status'] not in r.LIVE_STATUSES or not row.get('container'):
        raise tasks.TaskError('This restore has no scratch database any more')
    _require_docker()
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    info = _json(row.get('info'), {})
    dbs = _restored_databases(row, db_config)
    name = f'{ctx.id}.sql.gz'
    out = os.path.join(tasks.files_dir(), name)
    ctx.step(1, 1, f"Dumping {', '.join(dbs)} from {row['container']}")
    rc = run_local(ctx, rs.scratch_dump(engine, row['container'], dbs, out, info.get('superuser') or 'postgres'))
    if rc != 0:
        try:
            os.unlink(out)
        except OSError:
            pass
        raise tasks.TaskError(f'Dumping the scratch database failed (exit code {rc})')
    size = os.path.getsize(out)
    stamp = r.utc(row['target_time']).strftime('%Y%m%dT%H%M%SZ') if row.get('target_time') else row['id']
    return {'file': name, 'filename': f"{row['job_id']}-restored-{stamp}.sql.gz", 'mimetype': 'application/gzip',
            'size': size, 'download_url': f'/api/tasks/{ctx.id}/download', 'expires_in_hours': 24,
            'message': f'Dump ready ({r.fmt_bytes(size)})'}


@tasks.register('restore_remove', title='Remove a scratch restore', tier=tasks.TIER_SAFE, cancellable=False)
def _task_remove(ctx):
    row = _row_or_fail(ctx)
    ctx.step(1, 1, 'Removing the scratch copy')
    text = remove_scratch(row, log=ctx.log)
    status = row['status']
    new = r.S_REMOVED if status in (r.S_READY, r.S_RUNNING) else status
    update_row(row['id'], status=new, cleanup_at=None, container=row.get('container'))
    _m()._audit('RESTORE_REMOVE', 'job', row['job_id'], row.get('job_name'), {'restore_id': row['id']},
                user=ctx.created_by, ip=ctx.ip)
    return {'message': text or 'Removed', 'restore_id': row['id']}


# ---------------------------------------------------------------------------
# Replace production / undo
# ---------------------------------------------------------------------------

def _prod_path(db_config, job):
    if (db_config.get('docker_container') or '').strip():
        return 'docker exec'
    return 'client image' if _client_image(job, db_config) else 'direct'


def replace_plan(row, action='replace'):
    """The plan a replace/undo shows and re-derives (raises RestoreError when not allowed)."""
    if not row:
        raise RestoreError('Restore not found', 'not_found', 404)
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    if engine not in r.DB_ENGINES:
        raise RestoreError('Only database restores can replace production', 'not_ready', 409)
    if not server:
        raise RestoreError('The job has no server; production is reached through it', 'not_ready', 409)
    cfg = r.settings()
    plan_ = _json(row.get('plan'), {})
    if action == 'replace':
        if row['status'] != r.S_READY or row.get('target_type') != 'scratch':
            raise RestoreError('Replace production needs a successful scratch restore of this point first',
                               'not_ready', 409)
        if row.get('cleanup_at') and r.utc(row['cleanup_at']) < r.utcnow():
            raise RestoreError('This scratch restore has expired', 'not_ready', 409)
        point = (f"{r.fmt_time(row['target_time'])} ({plan_.get('method')})" if row.get('target_time')
                 else f"snapshot {str(row.get('snapshot_id'))[:8]}")
        source = {'container': row['container'], 'point': point}
        databases = _restored_databases(row, db_config)
    else:
        pre = _json(row.get('pre_restore_snapshot'), None)
        if row['status'] != r.S_REPLACED or not pre:
            raise RestoreError('Undo is only possible after a replace-production with a pre-restore backup',
                               'not_ready', 409)
        if row.get('pre_restore_forgotten_at'):
            raise RestoreError('The pre-restore backup has expired', 'not_ready', 409)
        first = pre[0]
        source = {'container': 'a new scratch container', 'point':
                  f"pre-restore snapshot {first['snapshot_id'][:8]} ({first.get('storage_name')}) from "
                  f"{r.fmt_time(row.get('pre_restore_at'))}"}
        databases = list(_json(row.get('info'), {}).get('databases') or r.configured_databases(db_config))
    est = int(plan_.get('estimated_restored_bytes') or 0)
    container = (db_config.get('docker_container') or '').strip() or None
    path = _prod_path(db_config, job)
    warnings = []
    if engine != r.ENGINE_POSTGRES and path != 'docker exec':
        warnings.append('DEFINER clauses are removed from views, triggers and routines (the import runs as '
                        f"{db_config.get('username')}), so they run as that user.")
    if not r.configured_databases(db_config):
        warnings.append('Databases that exist in production but not in the restored copy are left as they are.')
    if engine == r.ENGINE_POSTGRES:
        warnings.append('Sessions connected to these databases are terminated before the import; roles are '
                        'not changed.')
    steps = []
    if action == 'undo':
        steps.append(f"Restore {source['point']} into a new scratch container")
    steps += [
        f"Back up the current production database with the job's normal backup, tagged {r.PRE_RESTORE_TAG} "
        f"(kept {cfg['pre_restore_keep_days']} days); stop if it fails",
        f"Dump {', '.join(databases)} from the scratch container" +
        (' (with --set-gtid-purged=OFF)' if engine == r.ENGINE_MYSQL else ''),
        f"Copy the dump to {server.get('name') or server.get('host')}",
        'Save a rollback copy of the current production data on that server',
        f"Drop and re-import {', '.join(databases)} through {path}"
        + (f" (container {container})" if container else f" ({db_config.get('host')})"),
        'Compare row counts and key tables with the scratch copy',
        'If the import or the key-table check fails: import the rollback copy back',
    ]
    plan = {
        'restore_id': row['id'], 'action': action, 'job_id': row['job_id'], 'job_name': job.get('name'),
        'engine': engine,
        'production': {'database_config': db_config.get('name'), 'host': db_config.get('host'),
                       'container': container, 'databases': databases, 'path': path,
                       'server': server.get('name') or server.get('host')},
        'source': source, 'steps': steps,
        'downtime': r.downtime_text(databases, est, cfg),
        'pre_restore': (f"backup of the current production database, tagged {r.PRE_RESTORE_TAG}, kept "
                        f"{cfg['pre_restore_keep_days']} days (Undo restores it)"),
        'rollback': 'if the import or the key-table check fails, the rollback copy is imported back automatically',
        'warnings': warnings,
    }
    return plan, r.confirm_text(db_config)


def start_replace(restore_id, data, action, user=None, ip=None):
    """Preview -> {plan, plan_hash, confirm_text}; confirmed -> task row. Step-up is the route's job."""
    row = get_row(restore_id)
    plan, phrase = replace_plan(row, action)
    if (data or {}).get('preview'):
        return {'preview': security.present_plan(plan, phrase)}
    try:
        security.verify_confirmation(plan, (data or {}).get('plan_hash'), (data or {}).get('confirm'), phrase)
    except security.ConfirmationError as e:
        extra = security.present_plan(plan, phrase) if e.code == 'plan_changed' else {}
        raise RestoreError(str(e), e.code, e.status, extra)
    if _active_task_for_job(row['job_id']):
        raise RestoreError('A restore of this job is already running', 'busy', 409)
    kind = 'restore_replace' if action == 'replace' else 'restore_undo'
    title = (f"Replace production: {plan['job_name']}" if action == 'replace'
             else f"Undo replace-production: {plan['job_name']}")
    task, _ = tasks.enqueue(kind, {'restore_id': restore_id, 'job_id': row['job_id'],
                                   'plan_hash': security.plan_hash(plan)},
                            title=title, resource_type='job', resource_id=row['job_id'], created_by=user, ip=ip,
                            tier=tasks.TIER_MAINTENANCE, idempotency_key=f'{kind}:{restore_id}')
    update_row(restore_id, **({'replace_task_id': task['id']} if action == 'replace' else {'undo_task_id': task['id']}))
    _m()._audit('RESTORE_REPLACE' if action == 'replace' else 'RESTORE_UNDO', 'job', row['job_id'],
                plan['job_name'], {'restore_id': restore_id, 'phase': 'queued', 'task': task['id'],
                                   'databases': plan['production']['databases']}, user=user, ip=ip)
    return {'task': task}


UPLOAD_PATH = re.compile(r'^/[\w./-]*backupx-restore\.\w+$')


def _upload(server, local_path, timeout):
    m = _m()
    ssh_cmd = m._build_ssh_cmd_for_server(server, timeout=30)
    with open(local_path, 'rb') as f:
        proc = subprocess.run(list(ssh_cmd) + [rs.UPLOAD_SHELL], stdin=f, capture_output=True, timeout=timeout,
                              env=ssh_cmd.env() if hasattr(ssh_cmd, 'env') else None)
    remote = (proc.stdout or b'').decode(errors='replace').strip().splitlines()
    remote = remote[-1] if remote else ''
    if proc.returncode != 0 or not UPLOAD_PATH.match(remote):
        raise tasks.TaskError('Copying the dump to the server failed: ' +
                              (proc.stderr or b'').decode(errors='replace').strip()[-300:])
    return remote


def do_replace(ctx, row, action, plan, n0=0, m_total=7):
    """Pre-restore backup -> scratch dump -> upload -> rollback copy + import -> verify. Raises on failure."""
    m = _m()
    cfg = r.settings()
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    targets = m.get_job_storage_targets(job)
    ctx.add_secret(*m._job_secret_values(job, targets))
    databases = plan['production']['databases']
    info = _json(row.get('info'), {})

    def step(i, text):
        ctx.step(n0 + i, m_total, text)

    # 1. pre-restore backup (real run_backup; must succeed on every active storage)
    step(1, 'Pre-restore backup of production')
    saved = ctx.result
    ok, message = m.run_backup(row['job_id'], trigger='pre-restore', task=ctx,
                               tags=[r.PRE_RESTORE_TAG, f'restore:{row["id"]}'])
    backup_result, ctx.result = ctx.result, saved
    ctx.log(message, stream='system')
    if not ok:
        raise tasks.TaskError(f'The pre-restore backup failed, production was not touched: {message}')
    pre = [{'storage_id': s.get('id'), 'storage_name': s.get('name'), 'snapshot_id': s.get('snapshot_id')}
           for s in (backup_result or {}).get('storages', []) if s.get('snapshot_id')]
    if not pre:
        raise tasks.TaskError('The pre-restore backup reported no snapshot, production was not touched')
    update_row(row['id'], pre_restore_snapshot=pre, pre_restore_at=_naive(r.utcnow()))
    ctx.log('Pre-restore snapshot(s): ' + ', '.join(f"{p['storage_name']}: {p['snapshot_id'][:8]}" for p in pre),
            stream='system')

    remote = None
    work = os.path.join(cfg['work_dir'], row['id'] + '-replace')
    os.makedirs(work, mode=0o700, exist_ok=True)
    runner = rs.prod_runner(db_config, engine, _client_image(job, db_config))
    ssh_cmd = m._build_ssh_cmd_for_server(server, timeout=30)
    try:
        with ctx.lock(row['job_id'], wait=1800, what='This job (a backup is running)'):
            step(2, 'Dumping the restored data from the scratch container')
            out = os.path.join(work, 'restore.sql.gz')
            rc = run_local(ctx, rs.scratch_dump(engine, row['container'], databases, out,
                                                info.get('superuser') or 'postgres',
                                                strip_definers=plan['production']['path'] != 'docker exec'))
            if rc != 0:
                raise tasks.TaskError('Dumping the scratch database failed; production was not touched')
            step(3, f"Copying the dump to {plan['production']['server']}")
            remote = _upload(server, out, cfg['timeout'])
            markers = set()

            def on_line(_s, line):
                if line.startswith('BACKUPX-'):
                    markers.add(line.split()[0])
            proc = m._ssh_popen(ssh_cmd, rs.replace_script(runner, engine, databases, remote, n0 + 4, m_total))
            rc = ctx.stream(proc, timeout=cfg['timeout'], on_line=on_line)
            if rc != 0:
                if 'BACKUPX-ROLLBACK-OK' in markers:
                    raise tasks.TaskRolledBack('The import failed; production was put back from the rollback copy')
                if 'BACKUPX-ROLLBACK-FAILED' in markers:
                    raise tasks.TaskError('The import failed AND importing the rollback copy failed: production '
                                          'may be incomplete. Use Undo (pre-restore snapshot) or restore it by hand.')
                raise tasks.TaskError(f'The replace script failed before importing (exit code {rc}); '
                                      'production was not changed')
            step(6, 'Comparing production with the scratch copy')
            verify = run_verify(get_row(row['id']), phase='after_replace')
            mismatched = [t['name'] for t in verify['tables'] if t['key'] and t['match'] is False]
            if verify.get('prod_error'):
                mismatched = mismatched or ['(production could not be queried)']
            if mismatched:
                ctx.log('Key tables differ after the import: ' + ', '.join(mismatched), stream='system')
                res = m._ssh_run(ssh_cmd, rs.rollback_script(runner, engine, databases, remote), timeout=cfg['timeout'])
                ctx.log((res.stdout + res.stderr)[-2000:], stream='system')
                update_row(row['id'], verify=verify)
                if res.returncode == 0 and 'BACKUPX-ROLLBACK-OK' in res.stdout:
                    raise tasks.TaskRolledBack('Key tables differed after the import; production was put back '
                                               'from the rollback copy')
                raise tasks.TaskError('Key tables differed after the import and the rollback failed: use Undo')
            update_row(row['id'], verify=verify)
            ctx.log(_verify_line(verify), stream='system')
            if verify.get('health_url'):
                h = verify['health_url']
                ctx.log(f"Health URL {h['url']}: {'ok' if h['ok'] else 'FAILED'} ({h.get('status') or h.get('error')})",
                        stream='system')
            step(7, 'Done')
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if remote:
            try:
                m._ssh_run(ssh_cmd, rs.cleanup_remote_script(remote), timeout=120)
            except Exception:
                pass
    return pre


def _check_task_plan(ctx, row, action):
    plan, _phrase = replace_plan(row, action)
    if security.plan_hash(plan) != ctx.params.get('plan_hash'):
        raise tasks.TaskError('The plan changed after it was confirmed; nothing was done. Review it again.')
    return plan


@tasks.register('restore_replace', title='Replace production', tier=tasks.TIER_MAINTENANCE, cancellable=False)
def _task_replace(ctx):
    row = _row_or_fail(ctx)
    plan = _check_task_plan(ctx, row, 'replace')
    m = _m()
    update_row(row['id'], status=r.S_REPLACING, error=None)
    try:
        with ctx.lock(f"{row['job_id']}:restore", wait=60, what='Another restore of this job'):
            pre = do_replace(ctx, row, 'replace', plan)
    except BaseException as e:
        update_row(row['id'], status=r.S_READY, error=str(e)[:1000] or type(e).__name__)
        m._audit('RESTORE_REPLACE', 'job', row['job_id'], row.get('job_name'),
                 {'restore_id': row['id'], 'phase': 'finished', 'task': ctx.id}, status='failure',
                 error=str(e)[:300], user=ctx.created_by, ip=ctx.ip)
        raise
    update_row(row['id'], status=r.S_REPLACED, error=None)
    m._audit('RESTORE_REPLACE', 'job', row['job_id'], row.get('job_name'),
             {'restore_id': row['id'], 'phase': 'finished', 'task': ctx.id, 'pre_restore': pre},
             user=ctx.created_by, ip=ctx.ip)
    return {'message': f"Production replaced with {plan['source']['point']}; pre-restore snapshot "
                       f"{pre[0]['snapshot_id'][:8]} kept {r.settings()['pre_restore_keep_days']} days",
            'restore_id': row['id'], 'pre_restore_snapshot': pre}


@tasks.register('restore_undo', title='Undo replace-production', tier=tasks.TIER_MAINTENANCE, cancellable=False)
def _task_undo(ctx):
    orig = _row_or_fail(ctx)
    plan = _check_task_plan(ctx, orig, 'undo')
    m = _m()
    pre = _json(orig['pre_restore_snapshot'], [])[0]
    update_row(orig['id'], status=r.S_UNDOING, error=None)
    child = None
    try:
        with ctx.lock(f"{orig['job_id']}:restore", wait=60, what='Another restore of this job'):
            child_plan, _err = derive_plan({'job_id': orig['job_id'], 'point_type': r.POINT_SNAPSHOT,
                                            'snapshot_id': pre['snapshot_id'], 'storage_id': pre['storage_id'],
                                            'target': r.TARGET_SCRATCH})
            child_id = tasks.new_id()
            child = create_row({
                'id': child_id, 'parent_id': orig['id'], 'task_id': ctx.id, 'job_id': orig['job_id'],
                'job_name': orig.get('job_name'), 'engine': orig['engine'], 'point_type': r.POINT_SNAPSHOT,
                'snapshot_id': pre['snapshot_id'], 'target_time': _naive(child_plan.get('target_time')),
                'storage_id': pre['storage_id'], 'target_type': 'scratch', 'server': 'backupx',
                'container': r.container_name(child_id), 'plan': child_plan,
                'info': {'undo_of': orig['id'], 'databases': plan['production']['databases'],
                         'compare_production': True},  # Undo replaces production anyway
                'status': r.S_RUNNING, 'created_by': ctx.created_by})
            child = do_scratch(ctx, child)
            child_rplan = dict(plan, restore_id=child['id'],
                               source={'container': child['container'], 'point': plan['source']['point']})
            update_row(child['id'], status=r.S_REPLACING)
            new_pre = do_replace(ctx, get_row(child['id']), 'undo', child_rplan)
    except BaseException as e:
        update_row(orig['id'], status=r.S_REPLACED, error=f'Undo failed: {str(e)[:900]}')
        if child:
            c = get_row(child['id'])
            if c and c['status'] == r.S_REPLACING:
                update_row(child['id'], status=r.S_READY, error=str(e)[:900])
        m._audit('RESTORE_UNDO', 'job', orig['job_id'], orig.get('job_name'),
                 {'restore_id': orig['id'], 'phase': 'finished', 'task': ctx.id}, status='failure',
                 error=str(e)[:300], user=ctx.created_by, ip=ctx.ip)
        raise
    update_row(orig['id'], status=r.S_UNDONE)
    update_row(child['id'], status=r.S_REPLACED)
    m._audit('RESTORE_UNDO', 'job', orig['job_id'], orig.get('job_name'),
             {'restore_id': orig['id'], 'phase': 'finished', 'task': ctx.id, 'pre_restore': new_pre,
              'undo_restore': child['id']}, user=ctx.created_by, ip=ctx.ip)
    return {'message': f"Production put back to the pre-restore snapshot {pre['snapshot_id'][:8]}",
            'restore_id': child['id'], 'pre_restore_snapshot': new_pre}


# ---------------------------------------------------------------------------
# File restores
# ---------------------------------------------------------------------------

def files_plan(data):
    data = data or {}
    job, db_config, server, engine, state = job_context(data.get('job_id') or '')
    if engine != r.ENGINE_FILES:
        raise RestoreError('File restores are for filesystem jobs; use the database restore', 'invalid')
    target_kind = data.get('target') or 'alternate'
    if target_kind not in r.FILE_TARGETS:
        raise RestoreError('target must be original, alternate or scratch', 'invalid')
    target = _storage(job, data.get('storage_id'))
    m = _m()
    try:
        snap = _find_snapshot(m.get_snapshots(job, target), data.get('snapshot_id') or '')
    except Exception as e:
        raise RestoreError(f"Can't list snapshots: {m.sanitize_error_message(str(e))}", 'not_restorable')
    if not snap:
        raise RestoreError('Snapshot not found on this storage', 'not_restorable')
    paths, err = r.validate_restore_paths(data.get('paths') or [], snap.get('paths') or job.get('directories'))
    if err:
        raise RestoreError(err, 'invalid')
    cfg = r.settings()
    if target_kind in ('original', 'alternate') and (not server or not server.get('host')):
        raise RestoreError('The job has no server with SSH configured', 'invalid')
    if target_kind == 'original':
        target_path = '/'
    elif target_kind == 'alternate':
        target_path = (data.get('target_path') or '').strip() or r.default_alternate_path(job['id'])
        if not target_path.startswith('/') or '..' in target_path.split('/') or target_path == '/':
            raise RestoreError('The alternate path must be an absolute directory (not /)', 'invalid')
        target_path = os.path.normpath(target_path)
    else:
        target_path = None
    where = {'original': f"original location on {server.get('name') if server else '?'} (files are overwritten)",
             'alternate': f"{target_path} on {server.get('name') if server else '?'}",
             'scratch': f"{cfg['host_files_dir']}/{job['id']}-<time> on the backupx server"}[target_kind]
    steps = []
    if target_kind == 'original':
        steps.append(f"Pre-restore backup of the current versions of these paths on {server.get('name')} "
                     f"(tag {r.PRE_RESTORE_TAG}, kept {cfg['pre_restore_keep_days']} days)")
    steps.append(f'Restore {len(paths)} path(s) from snapshot {snap["id"][:8]} to {where}')
    plan = {
        'job_id': job['id'], 'job_name': job.get('name'), 'engine': engine, 'method': 'files',
        'point_type': r.POINT_SNAPSHOT, 'target': target_kind, 'files_target': target_kind,
        'target_time': snap.get('time'), 'storage': {'id': target['id'], 'name': target['name']},
        'dump': {'snapshot_id': snap['id'], 'time': snap.get('time'), 'size': r.snapshot_size(snap), 'path': None,
                 'paths': snap.get('paths') or []},
        'paths': paths, 'target_path': target_path, 'where': where, 'steps': steps,
        'server': server.get('name') if server else None, 'warnings': [],
    }
    if target_kind == 'original':
        plan['warnings'].append('Existing files at these paths are overwritten with the snapshot versions.')
    return plan, ('overwrite' if target_kind == 'original' else None)


def start_files(data, user=None, ip=None):
    plan, phrase = files_plan(data)
    try:
        security.verify_confirmation(plan, (data or {}).get('plan_hash'), (data or {}).get('confirm'), phrase)
    except security.ConfirmationError as e:
        extra = security.present_plan(plan, phrase) if e.code == 'plan_changed' else {}
        raise RestoreError(str(e), e.code, e.status, extra)
    restore_id = tasks.new_id()
    row = create_row({
        'id': restore_id, 'job_id': plan['job_id'], 'job_name': plan['job_name'], 'engine': r.ENGINE_FILES,
        'point_type': r.POINT_SNAPSHOT, 'snapshot_id': plan['dump']['snapshot_id'],
        'target_time': _naive(plan.get('target_time')), 'storage_id': plan['storage']['id'],
        'target_type': plan['target'], 'server': 'backupx' if plan['target'] == 'scratch' else plan.get('server'),
        'work_dir': plan.get('target_path'), 'plan': plan, 'info': {'files_target': plan['target']},
        'status': r.S_RUNNING, 'created_by': user})
    task, _ = tasks.enqueue('restore_files_paths', {'restore_id': restore_id, 'job_id': plan['job_id']},
                            title=f"Restore {len(plan['paths'])} path(s) of {plan['job_name']} ({plan['target']})",
                            resource_type='job', resource_id=plan['job_id'], created_by=user, ip=ip,
                            tier=tasks.TIER_CONFIRM, idempotency_key=f'restore:{restore_id}')
    update_row(restore_id, task_id=task['id'])
    _m()._audit('RESTORE_FILES', 'job', plan['job_id'], plan['job_name'],
                {'restore_id': restore_id, 'phase': 'queued', 'target': plan['target'], 'paths': plan['paths'],
                 'target_path': plan.get('target_path')}, user=user, ip=ip)
    return get_row(restore_id), task


@tasks.register('restore_files_paths', title='Restore files', tier=tasks.TIER_CONFIRM, cancellable=True)
def _task_files(ctx):
    row = _row_or_fail(ctx)
    plan = _json(row['plan'], {})
    m = _m()
    cfg = r.settings()
    if plan.get('files_target') == 'scratch' or plan.get('target') == r.TARGET_SCRATCH:
        row = do_scratch(ctx, row)
        return {'message': f"Extracted to {row.get('work_dir')} on the backupx server", 'restore_id': row['id']}
    job, db_config, server, engine, state = job_context(row['job_id'])
    engine = row.get('engine') or engine  # the dump header may have corrected it (MariaDB)
    target = _storage(job, row.get('storage_id'))
    ctx.add_secret(*m._job_secret_values(job, [target]))
    pre_restore = plan['target'] == 'original'
    script = rs.files_restore_script(job, target, m.job_repo_url(job, target), plan['dump']['snapshot_id'],
                                     plan['paths'], plan['target_path'], pre_restore, row['id'])
    found = {}

    def on_line(_s, line):
        if line.startswith('BACKUPX-PRE-RESTORE-SNAPSHOT '):
            found['snap'] = line.split(' ', 1)[1].strip()
    try:
        with ctx.repo_locks([m.job_repo_url(job, target)]):
            proc = m._ssh_popen(m._build_ssh_cmd_for_server(server, timeout=30), script)
            rc = ctx.stream(proc, timeout=cfg['timeout'], on_line=on_line)
    except BaseException as e:
        update_row(row['id'], status=r.S_CANCELLED if isinstance(e, tasks.TaskCancelled) else r.S_FAILED,
                   error=str(e)[:500])
        raise
    fields = {}
    if found.get('snap'):
        fields = {'pre_restore_snapshot': [{'storage_id': target['id'], 'storage_name': target['name'],
                                            'snapshot_id': found['snap']}],
                  'pre_restore_at': _naive(r.utcnow())}
        m.invalidate_snapshot_cache(job['id'])
    if rc != 0:
        update_row(row['id'], status=r.S_FAILED, error=f'exit code {rc}', **fields)
        m._audit('RESTORE_FILES', 'job', row['job_id'], row.get('job_name'),
                 {'restore_id': row['id'], 'phase': 'finished', 'task': ctx.id}, status='failure',
                 error=f'exit {rc}', user=ctx.created_by, ip=ctx.ip)
        raise tasks.TaskError(f'The file restore failed (exit code {rc}); see the log')
    update_row(row['id'], status=r.S_DONE, error=None, **fields)
    m._audit('RESTORE_FILES', 'job', row['job_id'], row.get('job_name'),
             {'restore_id': row['id'], 'phase': 'finished', 'task': ctx.id,
              'pre_restore_snapshot': found.get('snap')}, user=ctx.created_by, ip=ctx.ip)
    msg = f"Restored {len(plan['paths'])} path(s) to {plan['where']}"
    if found.get('snap'):
        msg += f"; previous versions in pre-restore snapshot {found['snap'][:8]}"
    return {'message': msg, 'restore_id': row['id'], 'pre_restore_snapshot': found.get('snap')}


def find_in_snapshot(job_id, snapshot_id, query, storage_id=None):
    m = _m()
    job, *_rest = job_context(job_id)
    target = _storage(job, storage_id)
    q = (query or '').strip()
    if len(q) < 2:
        raise RestoreError('Type at least 2 characters', 'invalid')
    if not re.match(r'^[\w .*?@+%-]+$', q):
        raise RestoreError('Only letters, digits, spaces and . _ - * ? are allowed', 'invalid')
    pattern = q if any(c in q for c in '*?') else f'*{q}*'
    try:
        proc = procs.run_gently(['restic', 'find', '--json', '-i', '--snapshot', snapshot_id, pattern, '--no-lock',
                                 *_insecure(target)], timeout=max(120, m.restic_read_timeout()),
                                env=_restic_env(job, target))
    except subprocess.TimeoutExpired:
        raise RestoreError('Searching the snapshot timed out; try a more specific name', 'restic_error')
    if proc.returncode != 0:
        raise RestoreError(m.sanitize_error_message(proc.stderr.strip() or 'restic find failed'), 'restic_error')
    limit = r.settings()['find_limit']
    matches = []
    try:
        data = json.loads(proc.stdout or '[]') or []
    except ValueError:
        data = []
    for block in data if isinstance(data, list) else [data]:
        for item in block.get('matches') or []:
            matches.append({'path': item.get('path'), 'type': 'dir' if item.get('type') == 'dir' else 'file',
                            'size': item.get('size') or 0, 'mtime': item.get('mtime')})
    return {'matches': matches[:limit], 'truncated': len(matches) > limit}


# ---------------------------------------------------------------------------
# Cleanup (leader, every RESTORE_CLEANUP_MINUTES)
# ---------------------------------------------------------------------------

def schedule_cleanup():
    m = _m()
    with m._short_lived_db():
        try:
            tasks.enqueue('restore_cleanup', {'scheduled': True}, title='Restore cleanup',
                          resource_type='system', resource_id='restores', idempotency_key='restore_cleanup')
        except Exception as e:
            logger.error(f"Could not queue the restore cleanup: {e}")
        finally:
            try:
                _db().close()
            except Exception:
                pass


def cleanup(now=None, log=None, docker_list=None):
    """Remove expired scratch copies, leftovers of dead tasks and orphan containers; expire
    pre-restore snapshots after PRE_RESTORE_KEEP_DAYS. Returns a summary dict."""
    cfg = r.settings()
    now = now or r.utcnow()
    log = log or (lambda t: logger.info(t))
    summary = {'expired': [], 'interrupted': [], 'orphans': [], 'pre_restore_forgotten': []}
    for row in list_rows(active=True, limit=200):
        task = tasks.get_task(row['task_id']) if row.get('task_id') else None
        task_dead = row['status'] in (r.S_RUNNING, r.S_REPLACING, r.S_UNDOING) and \
            (task is None or task['status'] in tasks.FINAL_STATUSES)
        if row['status'] == r.S_RUNNING and task_dead:
            log(f"Restore {row['id']}: its task {row.get('task_id')} is no longer running - removing leftovers")
            remove_scratch(row, log=log)
            update_row(row['id'], status=r.S_FAILED,
                       error=(task or {}).get('error') or 'interrupted (worker restarted)')
            summary['interrupted'].append(row['id'])
            continue
        if row.get('cleanup_at') and r.utc(row['cleanup_at']) <= now and \
                row['status'] in (r.S_READY, r.S_REPLACED, r.S_UNDONE):
            log(f"Restore {row['id']}: kept until {r.iso(row['cleanup_at'])} - removing the scratch copy")
            remove_scratch(row, log=log)
            update_row(row['id'], status=r.S_EXPIRED if row['status'] == r.S_READY else row['status'],
                       cleanup_at=None, container=row.get('container'))
            summary['expired'].append(row['id'])
    # Orphan containers (row gone or finished) - only when docker is reachable
    if docker_list is None and shutil.which('docker'):
        try:
            res = capture_local(rs.LIST_CONTAINERS, timeout=60)
            docker_list = [ln.split('\t') for ln in res.stdout.splitlines() if ln.strip()] if res.returncode == 0 else []
        except Exception:
            docker_list = []
    for parts in docker_list or []:
        rid = parts[0]
        row = get_row(rid)
        if row and row['status'] in r.LIVE_STATUSES:
            continue
        log(f"Orphan scratch container {parts[1] if len(parts) > 1 else rid} (restore {rid}) - removing")
        if shutil.which('docker'):
            capture_local(rs.remove_containers_script(rid), timeout=120)
        summary['orphans'].append(rid)
    # Work directories nobody owns
    try:
        live = {x['id'] for x in list_rows(active=True, limit=200)}
        for name in os.listdir(cfg['work_dir']):
            base = name.split('-replace')[0]
            if base not in live and os.path.isdir(os.path.join(cfg['work_dir'], name)):
                shutil.rmtree(os.path.join(cfg['work_dir'], name), ignore_errors=True)
    except OSError:
        pass
    summary['pre_restore_forgotten'] = expire_pre_restore(now, log)
    return summary


def expire_pre_restore(now=None, log=None):
    """Forget pre-restore snapshots older than PRE_RESTORE_KEEP_DAYS (only when retention is enabled)."""
    m = _m()
    cfg = r.settings()
    now = now or r.utcnow()
    log = log or (lambda t: logger.info(t))
    enabled = os.environ.get('RETENTION_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
    dry = os.environ.get('RETENTION_DRY_RUN', 'false').lower() in ('1', 'true', 'yes', 'on')
    cutoff = _naive(now - timedelta(days=cfg['pre_restore_keep_days']))
    rows = _db().fetchall('SELECT * FROM restores WHERE pre_restore_at IS NOT NULL AND pre_restore_at < %s '
                          'AND pre_restore_forgotten_at IS NULL', (cutoff,))
    done = []
    for row in rows:
        row = dict(row)
        if not enabled or dry:
            log(f"Restore {row['id']}: pre-restore snapshot past {cfg['pre_restore_keep_days']} days, kept "
                f"(retention {'dry run' if dry else 'off'})")
            continue
        job = m.get_job(row['job_id'])
        if not job:
            update_row(row['id'], pre_restore_forgotten_at=_naive(now))
            continue
        job = dict(job, id=row['job_id'])
        ok = True
        for snap in _json(row['pre_restore_snapshot'], []):
            target = m.resolve_job_storage(job, snap.get('storage_id'))
            if not target:
                continue
            res = m._restic_local(job, target, ['forget', snap['snapshot_id']], timeout=900,
                                  lock_wait=os.environ.get('RETENTION_LOCK_WAIT', '10m'))
            if res.returncode != 0 and 'no matching ID' not in (res.stderr or ''):
                ok = False
                log(f"Restore {row['id']}: forgetting pre-restore snapshot {snap['snapshot_id'][:8]} on "
                    f"{target['name']} failed: {(res.stderr or '').strip()[-200:]}")
        if ok:
            update_row(row['id'], pre_restore_forgotten_at=_naive(now))
            m.invalidate_snapshot_cache(row['job_id'])
            done.append(row['id'])
    return done


@tasks.register('restore_cleanup', title='Restore cleanup', tier=tasks.TIER_SAFE, cancellable=False)
def _task_cleanup(ctx):
    summary = cleanup(log=lambda t: ctx.log(t, stream='system'))
    n = sum(len(v) for v in summary.values())
    # noop: a scheduled tick that found nothing stays in the task list, not the dashboard activity
    return {'message': f'{n} item(s) cleaned up' if n else 'Nothing to clean up', 'noop': not n, **summary}

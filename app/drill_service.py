"""
Restore drills: the weekly "can we really restore it?" proof for PITR jobs.

Backups only - a drill never connects to production (no SSH, no SQL, no PITR
tick; tests/test_drill.py enforces it). Task `restore_drill` (one job) /
`restore_drill_all` (the weekly schedule, one job at a time, off-hours), all on
the backupx server with the P2 scratch-restore machinery, one scratch copy at a
time:

  A. newest anchored dump alone (MySQL/MariaDB) / newest WAL-G base backup alone
     (PostgreSQL, recovery_target = immediate) -> checksum every table -> remove;
  B. point-in-time restore to that backup's own point (dump + binlogs to the
     anchor time / base + WAL to the base's finish time) -> checksum -> remove;
     A and B must match (app.drill.compare_copies);
  C. point-in-time restore to the latest shipped point -> must apply cleanly,
     start, and every table must answer COUNT(*) -> remove.

The result goes to health_probes `drill:<job>` (dashboard "last verified
restore") and the job history; a failed drill sends a critical alert and shows
up as a health issue. Not enough scratch disk = skipped with a warning.
"""

import json
import logging
from datetime import datetime, timezone

from . import drill as d, restore as r, restore_scripts as rs, tasks

logger = logging.getLogger(__name__)

PROBE_PREFIX = 'drill:'
SCHEDULE_ID = '__restore_drill__'


def _m():
    from . import main
    return main


def _svc():
    from . import restore_service
    return restore_service


def _now():
    return datetime.now(timezone.utc)


def last_result(job_id):
    from . import health_service
    probe = health_service.load_probes().get(PROBE_PREFIX + job_id)
    return (probe or {}).get('data') or None


def results(job_ids=None):
    from . import health_service
    out = {}
    for key, probe in health_service.load_probes().items():
        if key.startswith(PROBE_PREFIX):
            jid = key[len(PROBE_PREFIX):]
            if job_ids is None or jid in job_ids:
                out[jid] = probe.get('data') or {}
    return out


def _save(job_id, result):
    from . import health_service
    health_service.save_probe(PROBE_PREFIX + job_id, result)


def _scratch_sql(row, engine, body, timeout):
    info = _svc()._json(row.get('info'), {})
    script = rs.PREAMBLE + rs.scratch_runner(engine, row['container'], info.get('superuser') or 'postgres') + body
    res = _svc().capture_local(script, timeout=timeout)
    return r.parse_marked_output(res.stdout), res


def _list_tables(row, engine, databases):
    """Base tables of a scratch copy: [(db, table, estimated rows)]."""
    if engine == r.ENGINE_POSTGRES:
        dbs = databases
        if not dbs:
            marked, _ = _scratch_sql(row, engine, rs.RUN_SQL + 'run_sql dbs "" ' + rs.q(
                'SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1') + '\n', 120)
            dbs = [x.strip() for x in (marked.get('dbs') or (1, []))[1] if x.strip()]
        body = rs.RUN_SQL + ''.join(f'run_sql {rs.q("t:" + db)} {rs.q(db)} {rs.q(d.list_tables_sql(engine, []))}\n'
                                    for db in dbs)
    else:
        body = rs.RUN_SQL + f'run_sql t "" {rs.q(d.list_tables_sql(engine, databases))}\n'
    marked, res = _scratch_sql(row, engine, body, 300)
    tables = []
    for label, (rc, lines) in marked.items():
        if label.startswith('t'):
            if rc != 0:
                raise tasks.TaskError('Listing the tables of the scratch copy failed: ' + ' '.join(lines[-2:]))
            tables += d.parse_tables(lines)
    if not marked:
        raise tasks.TaskError('Listing the tables of the scratch copy failed: '
                              + (res.stderr.strip()[-300:] or 'no output'))
    return tables


def _checksums(row, engine, tables, big):
    marked, _res = _scratch_sql(row, engine, d.checksum_body(engine, tables, big), 6 * 3600)
    return d.parse_checksums(marked)


class DrillSkipped(Exception):
    """The drill can't run here right now (not enough scratch disk): a warning, not a failed drill."""


def _plan(svc, data):
    plan, error = svc.derive_plan(dict(data, target='scratch'))
    reason = d.disk_skip_reason(plan, error)
    if reason:
        raise DrillSkipped(reason)
    if error:
        raise tasks.TaskError(error)
    return plan


def _reference(svc, job_id):
    """The latest point-in-time plan (also the disk pre-check) and the backup the drill compares against."""
    latest = _plan(svc, {'job_id': job_id, 'point_type': r.POINT_LATEST})
    if latest['method'] == 'binlog':
        dump = latest.get('dump') or {}
        if not dump.get('snapshot_id') or not dump.get('time'):
            raise tasks.TaskError('the newest anchored dump has no snapshot on this storage')
        return latest, {'method': 'binlog', 'snapshot_id': dump['snapshot_id'], 'time': dump['time']}
    if latest['method'] == 'walg':
        wal = latest.get('wal') or {}
        if not wal.get('base_backup') or not wal.get('base_time'):
            raise tasks.TaskError("couldn't list the WAL-G base backups")
        # Restore targets are whole seconds; the base backup finished at a fraction of one
        # (12:04:42.16): its own point is the next whole second, else no base backup "finished before" it
        return latest, {'method': 'walg', 'base_backup': wal['base_backup'],
                        'time': r.iso(d.ceil_second(wal['base_time']))}
    raise tasks.TaskError('point-in-time recovery has no restorable window on this storage (the latest point is '
                          'a plain snapshot)')


def _restore(ctx, svc, job, plan, label):
    restore_id = tasks.new_id()
    row = svc.create_row({
        'id': restore_id, 'job_id': job['id'], 'job_name': job.get('name'), 'engine': plan['engine'],
        'point_type': plan['point_type'], 'snapshot_id': (plan.get('dump') or {}).get('snapshot_id'),
        'target_time': svc._naive(plan.get('target_time')), 'storage_id': plan['storage']['id'],
        'target_type': 'scratch', 'server': 'backupx', 'container': r.container_name(restore_id), 'plan': plan,
        'info': {'drill': label, 'compare_production': False}, 'status': r.S_RUNNING,
        'created_by': ctx.created_by or 'restore-drill', 'task_id': ctx.id})
    return svc.do_scratch(ctx, row, verify_production=False)


def _server_changed_at(job_id):
    """When the PITR ticks last saw production's binlog position move (backupx's own row)."""
    from . import pitr_service
    try:
        return svc_json(pitr_service.load_state(job_id).get('info')).get('server_changed_at')
    except Exception:
        return None


def svc_json(text):
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


def _drop(svc, ctx, row):
    if row is None:
        return
    try:
        svc.remove_scratch(row, log=lambda t: ctx.log(t, stream='system'))
    finally:
        svc.update_row(row['id'], status=r.S_REMOVED)


def run_drill(ctx, job_id):
    """One drill. Returns the result dict (also saved); never touches production."""
    svc = _svc()
    cfg = d.settings()
    started = _now()
    result = {'job_id': job_id, 'started_at': started.isoformat(), 'ok': False, 'task_id': ctx.id,
              'mode': 'backup-vs-backup'}
    current = None
    try:
        job, db_config, _server, engine, _state = svc.job_context(job_id)
        job = dict(job, id=job.get('id') or job_id)
        result['job_name'] = job.get('name')
        if not job.get('pitr_enabled'):
            raise tasks.TaskError('point-in-time recovery is off for this job')
        if engine not in r.DB_ENGINES:
            raise tasks.TaskError('only database jobs can be drilled')
        ctx.add_secret(*_m()._job_secret_values(job, _m().get_job_storage_targets(job)),
                       db_config.get('password') or '')
        databases = r.configured_databases(db_config)
        configured = svc.verify_config(job).get('drill_count_only_tables') or []

        ctx.progress(3, 'Drill: planning (and checking free disk for the scratch copies)')
        latest, ref = _reference(svc, job_id)
        storage_id = latest['storage']['id']
        result.update(target_time=ref['time'], method=ref['method'], storage=latest['storage'].get('name'))

        # A: the backup alone
        if ref['method'] == 'binlog':
            plan_a = _plan(svc, {'job_id': job_id, 'point_type': r.POINT_SNAPSHOT,
                                 'snapshot_id': ref['snapshot_id'], 'storage_id': storage_id})
            what_a = f"dump snapshot {ref['snapshot_id'][:8]} alone"
        else:
            plan_a = _plan(svc, {'job_id': job_id, 'point_type': 'time', 'target_time': ref['time'],
                                 'storage_id': storage_id})
            plan_a['recovery_target'] = 'immediate'
            plan_a['wal'] = dict(plan_a.get('wal') or {}, base_backup=ref['base_backup'])
            what_a = f"base backup {ref['base_backup']} alone (recovery_target immediate)"
        ctx.progress(10, f'Drill A: restoring the {what_a}')
        current = _restore(ctx, svc, job, plan_a, 'A')
        engine = current.get('engine') or engine
        tables = _list_tables(current, engine, databases)[:cfg['max_tables']]
        big = d.count_only_keys(engine, tables, configured, cfg['max_table_rows'])
        per_job = sorted(set(configured) & big)
        ctx.log(f'{len(tables)} tables ({len(big)} compared by row count only'
                + (f"; per job setting: {', '.join(per_job)}" if per_job else '') + ')', stream='system')
        ctx.progress(30, 'Drill A: checksumming the copy')
        a = _checksums(current, engine, tables, big)
        _drop(svc, ctx, current)
        current = None

        # B: the point-in-time path to the same point
        ctx.progress(40, f"Drill B: point-in-time restore to {ref['time']} (the backup's own point)")
        plan_b = _plan(svc, {'job_id': job_id, 'point_type': 'time', 'target_time': ref['time'],
                             'storage_id': storage_id})
        current = _restore(ctx, svc, job, plan_b, 'B')
        ctx.progress(60, 'Drill B: checksumming the copy')
        b = _checksums(current, engine, tables, big)
        # tables only B has (created by replayed events) count as differences too
        b_tables = {f'{t[0]}.{t[1]}' for t in _list_tables(current, engine, databases)}
        for key in sorted(b_tables - set(a)):
            b[key] = 'present'
        _drop(svc, ctx, current)
        current = None
        comparison = d.compare_copies(a, b)

        # C: the latest shipped point applies cleanly and every table reads
        ctx.progress(70, f"Drill C: point-in-time restore to the latest point {latest.get('target_time')}")
        plan_c = _plan(svc, {'job_id': job_id, 'point_type': 'time', 'target_time': latest['target_time'],
                             'storage_id': storage_id})
        current = _restore(ctx, svc, job, plan_c, 'C')
        c_info = svc._json(current.get('info'), {})
        c_tables = _list_tables(current, engine, databases)[:cfg['max_tables']]
        ctx.progress(90, f'Drill C: reading {len(c_tables)} tables')
        counts = _checksums(current, engine, c_tables, {f'{t[0]}.{t[1]}' for t in c_tables})
        _drop(svc, ctx, current)
        current = None
        latest_check = dict(d.readable(counts), target_time=latest.get('target_time'))
        # ... and reaches as far as the window claims (backupx's records of production, no query)
        reach = d.latest_reaches(plan_c.get('method'), plan_c.get('target_time'),
                                 (plan_c.get('dump') or {}).get('time'), c_info.get('last_event_at'),
                                 _server_changed_at(job_id), int(job.get('pitr_interval') or 5) + 2)
        latest_check.update(reaches=reach['ok'], reaches_detail=reach['detail'],
                            last_event_at=c_info.get('last_event_at'))
        ctx.log(f"Drill C: newest replayed event {c_info.get('last_event_at') or 'unknown'}; "
                f"{reach['detail'] or 'reach not checked'}", stream='system')
        result.update(ok=comparison['ok'] and latest_check['ok'] and reach['ok'] is not False,
                      comparison=comparison, latest=latest_check)
    except tasks.TaskCancelled:
        result['error'] = 'cancelled'
        raise
    except DrillSkipped as e:
        result.update(ok=None, skipped=True, warning=_m().sanitize_error_message(str(e), 500))
        ctx.log(f"Drill skipped: {result['warning']}", stream='system')
    except Exception as e:
        result['error'] = _m().sanitize_error_message(str(e) or type(e).__name__, 500)
    finally:
        if current is not None:
            try:
                _drop(svc, ctx, current)
            except Exception as e:
                logger.error(f'Drill: removing scratch copy {current.get("id")} failed: {e}')
        result['finished_at'] = _now().isoformat()
        result['duration_s'] = int((_now() - started).total_seconds())
        result['summary'] = d.summary(result)
        _save(job_id, result)
        _report(job_id, result)
    return result


def _report(job_id, result):
    m = _m()
    name = result.get('job_name') or job_id
    if result.get('skipped'):
        status, verdict = 'skipped', 'skipped'
    else:
        status, verdict = ('success', 'passed') if result['ok'] else ('failed', 'FAILED')
    try:
        m.add_history(job_id, name, status, f"Restore drill {verdict}: {result['summary']}",
                      result.get('duration_s') or 0)
    except Exception as e:
        logger.warning(f'Drill history for {job_id} failed: {e}')
    if result.get('skipped'):
        logger.warning(f"Restore drill of {name} skipped: {result.get('warning')}")
        return
    if not result['ok'] and result.get('error') != 'cancelled':
        try:
            m.send_notification(job_id, f'{name} (restore drill)', 'failed',
                                m.sanitize_error_message(result['summary'], 1000), result.get('duration_s') or 0)
        except Exception as e:
            logger.error(f'Drill alert for {job_id} failed: {e}')


@tasks.register('restore_drill', title='Restore drill', tier=tasks.TIER_SAFE, cancellable=True)
def _task_drill(ctx):
    job_id = ctx.params['job_id']
    with ctx.lock(f'{job_id}:drill', wait=60, what='Another drill of this job'):
        result = run_drill(ctx, job_id)
    ctx.result = result
    if not result['ok'] and not result.get('skipped'):
        raise tasks.TaskError(result['summary'])
    return dict(result, message=result['summary'])


def drill_jobs():
    m = _m()
    out = []
    for job_id, job in (m.load_jobs() or {}).items():
        if job.get('pitr_enabled') and job.get('backup_type') == 'database':
            out.append(job_id)
    return sorted(out)


@tasks.register('restore_drill_all', title='Weekly restore drills', tier=tasks.TIER_SAFE, cancellable=True)
def _task_drill_all(ctx):
    ids = ctx.params.get('job_ids') or drill_jobs()
    passed, failed, skipped = [], [], []
    for n, job_id in enumerate(ids, 1):
        ctx.check_cancelled()
        ctx.log(f'--- Drill {n}/{len(ids)}: {job_id}', stream='system')
        try:
            with ctx.lock(f'{job_id}:drill', wait=600, what='Another drill of this job'):
                res = run_drill(ctx, job_id)
        except tasks.TaskBusy as e:
            ctx.log(str(e), stream='system')
            failed.append(job_id)
            continue
        (skipped if res.get('skipped') else passed if res['ok'] else failed).append(job_id)
        ctx.log(res['summary'], stream='system')
    result = {'passed': passed, 'failed': failed, 'skipped': skipped,
              'message': f'{len(passed)} drill(s) passed, {len(failed)} failed'
                         + (f', {len(skipped)} skipped (see warnings)' if skipped else '')}
    ctx.result = result
    if failed:
        raise tasks.TaskError(result['message'] + ': ' + ', '.join(failed))
    return result


def schedule_all():
    """Leader cron entry: queue the weekly drills (one task runs them one at a time)."""
    m = _m()
    with m._short_lived_db():
        try:
            if not d.settings()['enabled'] or not drill_jobs():
                return
            tasks.enqueue('restore_drill_all', {'scheduled': True}, title='Weekly restore drills',
                          resource_type='system', resource_id='drills', idempotency_key='restore_drill_all')
        except Exception as e:
            logger.error(f'Could not queue the restore drills: {e}')
        finally:
            try:
                m.get_db().close()
            except Exception:
                pass


def serialize(result):
    if not result:
        return None
    keys = ('ok', 'started_at', 'finished_at', 'target_time', 'summary', 'method', 'storage', 'duration_s',
            'task_id', 'error', 'skipped', 'warning', 'mode')
    out = {k: result.get(k) for k in keys}
    c = result.get('comparison') or {}
    out['tables_compared'] = c.get('compared')
    out['tables'] = c.get('tables')
    out['mismatches'] = [{'table': m.get('table'), 'production': m.get('backup', m.get('production')),
                          'restored': m.get('pitr', m.get('restored'))} for m in (c.get('mismatches') or [])]
    latest = result.get('latest') or {}
    out['latest'] = {k: latest.get(k) for k in ('ok', 'tables', 'errors', 'target_time', 'reaches',
                                                 'reaches_detail', 'last_event_at')} if latest else None
    return out


def to_json(result):
    return json.dumps(serialize(result))

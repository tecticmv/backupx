"""
Restore drills: the weekly "can we really restore it?" proof for PITR jobs.

Task `restore_drill` (one job) / `restore_drill_all` (the weekly schedule, one job
at a time, off-hours): checksum production (read-only) -> wait until the
binlogs/WAL covering "now" are shipped -> scratch restore to that second on the
backupx server (the P2 restore machinery, never production) -> checksum the copy
-> checksum production again -> compare (app.drill.compare) -> remove the copy.
The result goes to health_probes `drill:<job>` (dashboard "last verified
restore") and the job history; a failed drill sends a critical alert and shows
up as a health issue.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

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


def _prod(job, db_config, server, body, timeout):
    res = _svc().prod_sql(job, db_config, server, body, timeout=timeout)
    return r.parse_marked_output(res.stdout), res


def _list_tables(job, db_config, server, engine, databases):
    if engine == r.ENGINE_POSTGRES:
        dbs = databases
        if not dbs:
            marked, _ = _prod(job, db_config, server, rs.RUN_SQL + 'run_sql dbs "" ' + rs.q(
                'SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1') + '\n', 120)
            dbs = [x.strip() for x in (marked.get('dbs') or (1, []))[1] if x.strip()]
        body = rs.RUN_SQL + ''.join(f'run_sql {rs.q("t:" + db)} {rs.q(db)} {rs.q(d.list_tables_sql(engine, []))}\n'
                                    for db in dbs)
    else:
        body = rs.RUN_SQL + f'run_sql t "" {rs.q(d.list_tables_sql(engine, databases))}\n'
    marked, res = _prod(job, db_config, server, body, 300)
    tables = []
    for label, (rc, lines) in marked.items():
        if label.startswith('t'):
            if rc != 0:
                raise tasks.TaskError('Listing the production tables failed: ' + ' '.join(lines[-2:]))
            tables += d.parse_tables(lines)
    if not marked:
        raise tasks.TaskError('Listing the production tables failed: ' + (res.stderr.strip()[-300:] or 'no output'))
    return tables


def _checksums_prod(job, db_config, server, engine, tables, big):
    marked, _res = _prod(job, db_config, server, d.checksum_body(engine, tables, big), 6 * 3600)
    return d.parse_checksums(marked)


def _checksums_scratch(row, engine, tables, big):
    info = _svc()._json(row.get('info'), {})
    script = rs.PREAMBLE + rs.scratch_runner(engine, row['container'], info.get('superuser') or 'postgres') + \
        d.checksum_body(engine, tables, big)
    res = _svc().capture_local(script, timeout=6 * 3600)
    return d.parse_checksums(r.parse_marked_output(res.stdout))


class DrillSkipped(Exception):
    """The drill can't run here right now (not enough scratch disk): a warning, not a failed drill."""


def _disk_precheck(svc, job_id):
    """Refuse early (before reading production) when the backupx host lacks scratch disk."""
    try:
        plan, error = svc.derive_plan({'job_id': job_id, 'point_type': r.POINT_LATEST, 'target': 'scratch'})
    except Exception as e:  # the real plan below reports restorability problems
        logger.info(f'Drill disk pre-check for {job_id} could not plan: {e}')
        return
    reason = d.disk_skip_reason(plan, error)
    if reason:
        raise DrillSkipped(reason)


def _wait_shipped(ctx, job_id, target, minutes):
    from . import pitr_service
    deadline = time.monotonic() + minutes * 60
    while True:
        pitr_service.run_tick(job_id)
        state = pitr_service.load_state(job_id)
        end = r.utc(state.get('window_end')) if state.get('window_end') else None
        if end and end >= target:
            ctx.log(f'Shipped up to {end.isoformat()}', stream='system')
            return
        if time.monotonic() >= deadline:
            raise tasks.TaskError(f"PITR data doesn't reach {target.isoformat()} after {minutes} min "
                                  f"(state {state.get('state')}, window end {end})")
        ctx.log(f"Waiting for the binlogs/WAL up to {target.isoformat()} to be shipped "
                f"(window ends {end.isoformat() if end else 'nowhere'})", stream='system')
        if ctx.cancel_event.wait(20):
            raise tasks.TaskCancelled()


def run_drill(ctx, job_id):
    """One drill. Returns the result dict (also saved); raises on infrastructure errors only after saving."""
    svc = _svc()
    cfg = d.settings()
    started = _now()
    result = {'job_id': job_id, 'started_at': started.isoformat(), 'ok': False, 'task_id': ctx.id}
    row = None
    try:
        job, db_config, server, engine, state = svc.job_context(job_id)
        result['job_name'] = job.get('name')
        if not job.get('pitr_enabled'):
            raise tasks.TaskError('point-in-time recovery is off for this job')
        if engine not in r.DB_ENGINES:
            raise tasks.TaskError('only database jobs can be drilled')
        ctx.add_secret(*_m()._job_secret_values(job, _m().get_job_storage_targets(job)),
                       db_config.get('password') or '')
        databases = r.configured_databases(db_config)
        ctx.progress(3, 'Drill: checking free disk for the scratch copy')
        _disk_precheck(svc, job_id)
        ctx.progress(5, 'Drill: listing production tables')
        tables = _list_tables(job, db_config, server, engine, databases)[:cfg['max_tables']]
        configured = svc.verify_config(job).get('drill_count_only_tables') or []
        big = d.count_only_keys(engine, tables, configured, cfg['max_table_rows'])
        per_job = sorted(set(configured) & big)
        ctx.log(f'{len(tables)} tables ({len(big)} compared by row count only'
                + (f"; per job setting: {', '.join(per_job)}" if per_job else '') + ')', stream='system')
        ctx.progress(10, 'Drill: checksumming production (read-only)')
        before = _checksums_prod(job, db_config, server, engine, tables, big)
        target = (_now() + timedelta(seconds=1)).replace(microsecond=0) + timedelta(seconds=1)
        while _now() < target:
            time.sleep(0.2)
        result['target_time'] = target.isoformat()
        ctx.progress(20, f'Drill: waiting until {target.isoformat()} is shipped')
        _wait_shipped(ctx, job_id, target, cfg['ship_wait_minutes'])
        plan, error = svc.derive_plan({'job_id': job_id, 'point_type': 'time', 'target_time': target.isoformat(),
                                       'target': 'scratch'})
        reason = d.disk_skip_reason(plan, error)
        if reason:
            raise DrillSkipped(reason)
        if error:
            raise tasks.TaskError(error)
        restore_id = tasks.new_id()
        row = svc.create_row({
            'id': restore_id, 'job_id': job_id, 'job_name': job.get('name'), 'engine': plan['engine'],
            'point_type': 'time', 'snapshot_id': (plan.get('dump') or {}).get('snapshot_id'),
            'target_time': svc._naive(target), 'storage_id': plan['storage']['id'], 'target_type': 'scratch',
            'server': 'backupx', 'container': r.container_name(restore_id), 'plan': plan,
            'info': {'drill': True}, 'status': r.S_RUNNING, 'created_by': ctx.created_by or 'restore-drill',
            'task_id': ctx.id})
        result['restore_id'] = restore_id
        ctx.progress(30, f"Drill: scratch restore to {target.isoformat()} ({plan['method']})")
        row = svc.do_scratch(ctx, row)
        engine = row.get('engine') or engine
        ctx.progress(80, 'Drill: checksumming the restored copy')
        restored = _checksums_scratch(row, engine, tables, big)
        ctx.progress(90, 'Drill: checksumming production again')
        after = _checksums_prod(job, db_config, server, engine, tables, big)
        comparison = d.compare(before, after, restored)
        result.update(ok=comparison['ok'], comparison=comparison, method=plan['method'],
                      storage=plan['storage'].get('name'))
    except tasks.TaskCancelled:
        result['error'] = 'cancelled'
        raise
    except DrillSkipped as e:
        result.update(ok=None, skipped=True, warning=_m().sanitize_error_message(str(e), 500))
        ctx.log(f"Drill skipped: {result['warning']}", stream='system')
    except Exception as e:
        result['error'] = _m().sanitize_error_message(str(e) or type(e).__name__, 500)
    finally:
        if row is not None:
            svc.remove_scratch(row, log=lambda t: ctx.log(t, stream='system'))
            svc.update_row(row['id'], status=r.S_REMOVED)
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
            'task_id', 'error', 'skipped', 'warning')
    out = {k: result.get(k) for k in keys}
    c = result.get('comparison') or {}
    out['tables_compared'] = c.get('compared')
    out['tables'] = c.get('tables')
    out['mismatches'] = c.get('mismatches') or []
    return out


def to_json(result):
    return json.dumps(serialize(result))

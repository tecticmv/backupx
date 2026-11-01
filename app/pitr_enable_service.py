"""
Enable-PITR wizard: plans, the apply task and the maintenance (restart) flow.

Pure decisions live in app.pitr_enable, shell in app.maint_scripts. Flow of
one run (task `pitr_enable`, maintenance tier):

  Apply   no-downtime items (wal-g install, client charset fix, client image,
          GRANTs with a one-time admin login, SET PERSIST / SET GLOBAL + drop-in,
          ALTER SYSTEM + pg_reload_conf), then re-check
  Restart (now, or a child task `pitr_enable_restart` with tasks.not_before =
          the window) fresh backup if the last one is older than 1 h -> config
          backup -> write the drop-in / ALTER SYSTEM -> validate -> restart ->
          health check -> on failure restore the config, restart again, alert,
          task `rolled_back` (never while the server is still recovering)
  Stream  pitr_enabled on, streamer / wal-g installed, warm-up tick until the
          chain has the server's uuid (so the first dump can anchor it)
  Anchor  anchored dump (MySQL) / WAL-G base backup (PostgreSQL), ship, wait
          until the PITR state is ok

Each phase's task steps start with its name ("Apply: ...") so the wizard can
follow the run after a page reload (GET /api/jobs/:id/pitr/enable).
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import credentials, maint_scripts as ms, pitr, pitr_enable as pe, security, tasks

logger = logging.getLogger(__name__)

PROBE_PREFIX = 'pitrplan:'
TASK_ENABLE = 'pitr_enable'
TASK_RESTART = 'pitr_enable_restart'
ORDER = {'walg_install': 10, 'client_charset': 20, 'job_field': 30, 'grant': 40, 'sql': 50, 'dropin_sql': 60,
         'alter_system_reload': 70}


class EnableError(Exception):
    def __init__(self, message, code='invalid', status=400, extra=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}


def _m():
    from . import main
    return main


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Facts (prerequisite check + host probe), cached for the wizard
# ---------------------------------------------------------------------------

def job_context(job_id):
    from . import pitr_service
    try:
        return pitr_service._job_context(job_id)
    except pitr_service.PitrError as e:
        text = str(e)
        code = 'not_found' if 'not found' in text and 'job' in text else \
            ('no_server' if 'server' in text else 'not_database')
        raise EnableError(text, code, 404 if code == 'not_found' else 400)


def _run(server, script, timeout=120):
    m = _m()
    return m._ssh_run(m._build_ssh_cmd_for_server(server, timeout=30), script, timeout=timeout)


def gather(job_id):
    """Run the prerequisite check and the host probe. Returns the facts bundle (no secrets)."""
    from . import pitr_service
    job, server, db_config, kind = job_context(job_id)
    try:
        prereq = pitr_service.check_prereqs(job_id, job, with_facts=True)
    except pitr_service.PitrError as e:
        raise EnableError(str(e), 'ssh_failed', 502)
    script = ms.pg_host_probe_script() if kind == 'postgres' else ms.mysql_host_probe_script(db_config)
    res = _run(server, script, timeout=120)
    host = ms.parse(res.stdout)
    if res.returncode != 0 and not host:
        raise EnableError('Probing the server failed: ' + _m().sanitize_error_message(res.stderr.strip()[-300:]),
                          'ssh_failed', 502)
    # The grants text names the account; keep the rest of the facts small
    facts = {k: v for k, v in (prereq.get('facts') or {}).items() if len(str(v)) < 4000}
    bundle = {'kind': kind, 'checks': prereq['checks'], 'ok': prereq['ok'], 'facts': facts,
              'server_version': prereq.get('server_version'), 'docker': prereq.get('docker'),
              'suggested_client_image': prereq.get('suggested_client_image'),
              'host': host, 'checked_at': _now().isoformat()}
    from . import health_service
    health_service.save_probe(PROBE_PREFIX + job_id, bundle)
    return bundle


def cached(job_id, max_age=None):
    from . import health_service
    max_age = pe.settings()['plan_cache_seconds'] if max_age is None else max_age
    probe = health_service.load_probes().get(PROBE_PREFIX + job_id)
    if not probe:
        return None
    data = probe['data'] or {}
    at = pitr._utc(data.get('checked_at')) if data.get('checked_at') else None
    if not at or (_now() - at).total_seconds() > max_age:
        return None
    return data


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _target(kind, db_config, server, facts):
    host = facts.get('host') or {}
    if kind == 'postgres':
        prereq_facts = facts.get('facts') or {}
        return pe.pg_target(db_config, server, prereq_facts, host)
    return pe.mysql_target(db_config, server, {'server_version': facts.get('server_version')}, host)


def _items(kind, job, target, facts, restart_selected=None):
    prereq = {'checks': facts.get('checks'), 'facts': facts.get('facts'), 'docker': facts.get('docker'),
              'suggested_client_image': facts.get('suggested_client_image')}
    if kind == 'postgres':
        return pe.pg_items(prereq, target, facts.get('host') or {})
    return pe.mysql_items(prereq, target, facts.get('host') or {}, job, restart_selected=restart_selected)


def confirm_text(db_config, server):
    return (db_config.get('name') or server.get('name') or server.get('host') or 'confirm').strip()


def derive(job_id, facts, data):
    """(plan, response) for a selection. data: {items?, restart?, restart_at?, fresh_backup?}."""
    job, server, db_config, kind = job_context(job_id)
    target = _target(kind, db_config, server, facts)
    items = _items(kind, job, target, facts)
    selected = data.get('items')
    if selected is None:
        selected = pe.default_selection(items)
    if not isinstance(selected, list) or not all(isinstance(i, str) for i in selected):
        raise EnableError('items must be a list of item ids', 'invalid_items')
    restart_ids = {i['id'] for i in items if i['downtime'] == 'restart' and i['id'] in ('mysql.log_bin',
                                                                                         'mysql.server_id')}
    if kind == 'mysql':
        # MariaDB settings fold into the restart's drop-in when a restart happens anyway
        items = _items(kind, job, target, facts, restart_selected=bool(restart_ids & set(selected)))
    mode = data.get('restart') or 'now'
    at = None
    if mode == 'at':
        try:
            at = pe.parse_restart_at(data.get('restart_at'))
        except pe.PlanError as e:
            raise EnableError(str(e), e.code)
    try:
        plan = pe.build_plan(dict(job, id=job_id), kind, target, items, selected, mode, at,
                             bool(data.get('fresh_backup')))
    except pe.PlanError as e:
        raise EnableError(str(e), e.code)
    last = job.get('last_success')
    cfg = pe.settings()
    phrase = confirm_text(db_config, server)
    needs_admin = plan['admin']
    account = next((i.get('account') for i in items if i['id'] == 'mysql.grants'), None) or \
        pitr.mysql_account((facts.get('facts') or {}).get('user', ''))
    grant_sql = next(((i.get('sql') or [''])[0] + ';' for i in items if i['id'] == 'mysql.grants'), None)
    host = facts.get('host') or {}
    response = {
        'kind': kind,
        'job': {'id': job_id, 'name': job.get('name'), 'pitr_enabled': bool(job.get('pitr_enabled'))},
        'target': target,
        'checks': facts.get('checks') or [],
        'prereqs_ok': bool(facts.get('ok')),
        'items': items,
        'admin': {'needed': needs_admin,
                  'socket_available': kind == 'mysql' and target['install'] == 'host'
                  and (host.get('uid') == '0' or host.get('sudo_uid') == '0'),
                  'account': account if kind == 'mysql' else None, 'sql': grant_sql},
        'selection': {'items': [i['id'] for i in plan['items']], 'restart': mode,
                      'restart_at': at.isoformat() if at else None,
                      'fresh_backup': pe.fresh_backup_decision(last, max_age=cfg['fresh_backup_max_age'],
                                                               force=bool(data.get('fresh_backup'))),
                      'last_backup_at': last},
        'steps': plan['steps'],
        'downtime': plan['downtime'],
        'needs_restart': bool(plan['restart']),
        'checked_at': facts.get('checked_at'),
        'enable_run': current_run(job_id),
    }
    response.update(security.present_plan(plan, phrase))
    return plan, response


def plan_for(job_id, data):
    data = data or {}
    facts = None if data.get('refresh', True) else cached(job_id)
    if facts is None:
        facts = gather(job_id)
    return derive(job_id, facts, data)


# ---------------------------------------------------------------------------
# Start (route) and run state
# ---------------------------------------------------------------------------

def _active(job_id):
    db = tasks._get_db()
    return db.fetchone("SELECT * FROM tasks WHERE resource_type = 'job' AND resource_id = %s AND kind IN (%s, %s) "
                       "AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1",
                       (job_id, TASK_ENABLE, TASK_RESTART))


def start(job_id, data, user=None, ip=None):
    """Validate the confirmation and queue the run. Step-up is the route's job. Returns the task row."""
    data = data or {}
    facts = cached(job_id)
    if facts is None:
        raise EnableError('The check is too old; run the check again', 'stale_check')
    plan, response = derive(job_id, facts, data)
    phrase = response['confirm_text']
    try:
        security.verify_confirmation(plan, data.get('plan_hash'), data.get('confirm'), phrase)
    except security.ConfirmationError as e:
        extra = security.present_plan(plan, phrase) if e.code == 'plan_changed' else {}
        if e.code == 'plan_changed':
            extra['items'] = response['items']
        raise EnableError(str(e), e.code, e.status, extra)
    admin, error = credentials.validate_admin(data.get('admin'))
    if error:
        raise EnableError(error, 'invalid_admin')
    if plan['admin'] and not admin:
        raise EnableError('A selected item needs a one-time admin login (or run its SQL yourself and deselect it)',
                          'admin_required')
    if admin and admin['mode'] == 'socket' and not response['admin']['socket_available']:
        raise EnableError("The local socket login needs a host install and root/sudo on the server",
                          'invalid_admin')
    if not plan['admin']:
        admin = None
    active = _active(job_id)
    if active:
        raise EnableError('A PITR setup run for this job is already active', 'busy', 409,
                          {'task': tasks.serialize(active)})
    task_id = tasks.new_id()
    if admin:
        credentials.put(task_id, admin)
    params = {'job_id': job_id, 'items': [i['id'] for i in plan['items']], 'restart': response['selection']['restart'],
              'restart_at': response['selection']['restart_at'], 'fresh_backup': bool(data.get('fresh_backup')),
              'plan_hash': security.plan_hash(plan), 'uses_admin': bool(admin),
              'admin_mode': admin['mode'] if admin else None}
    try:
        row, created = tasks.enqueue(TASK_ENABLE, params, title=f"Enable PITR: {plan['job_name']}",
                                     resource_type='job', resource_id=job_id, created_by=user, ip=ip,
                                     tier=tasks.TIER_MAINTENANCE, idempotency_key=f'{TASK_ENABLE}:{job_id}',
                                     pinned_worker=tasks.local_worker_id() if admin else None, task_id=task_id)
    except Exception:
        credentials.discard(task_id)
        raise
    if not created:
        credentials.discard(task_id)
        raise EnableError('A PITR setup run for this job is already active', 'busy', 409,
                          {'task': tasks.serialize(row)})
    _m()._audit('PITR_ENABLE', 'job', job_id, plan['job_name'],
                {'phase': 'queued', 'task': task_id, 'items': params['items'], 'restart': params['restart'],
                 'restart_at': params['restart_at'], 'admin_login': params['admin_mode']}, user=user, ip=ip)
    return row


def current_run(job_id):
    """The newest enable run of a job (parent + scheduled child), for the wizard."""
    db = tasks._get_db()
    parent = db.fetchone("SELECT * FROM tasks WHERE kind = %s AND resource_type = 'job' AND resource_id = %s "
                         "ORDER BY created_at DESC LIMIT 1", (TASK_ENABLE, job_id))
    if not parent:
        return None
    child = db.fetchone("SELECT * FROM tasks WHERE kind = %s AND parent_id = %s ORDER BY created_at DESC LIMIT 1",
                        (TASK_RESTART, parent['id']))
    p, c = tasks.serialize(parent), tasks.serialize(child) if child else None
    active = c if c and (c['status'] in tasks.ACTIVE_STATUSES or p['status'] in tasks.FINAL_STATUSES) else p
    phase, scheduled = None, None
    if c and c['status'] == tasks.QUEUED:
        phase, scheduled = 'scheduled', (child.get('not_before') and
                                         pitr._utc(child['not_before']).isoformat())
    elif active['status'] in tasks.ACTIVE_STATUSES:
        phase = pe.phase_of_step(active.get('step')) or ('apply' if active is p else 'restart')
    elif active['status'] == tasks.SUCCEEDED:
        phase = 'done'
    elif active['status'] == tasks.ROLLED_BACK:
        phase = 'rolled_back'
    elif active['status'] == tasks.CANCELLED:
        phase = 'cancelled'
    else:
        phase = 'failed'
    return {'task': p, 'restart_task': c, 'phase': phase, 'scheduled_for': scheduled,
            'active_task_id': active['id'], 'result': active.get('result') if active['status'] in
            tasks.FINAL_STATUSES else None}


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

class PhaseTask:
    """A TaskContext seen through one phase: progress text from a backup run keeps the phase prefix."""

    def __init__(self, ctx, phase):
        self._ctx = ctx
        self._phase = phase

    def progress(self, pct=None, step=None):
        self._ctx.progress(None, f'{self._phase}: {step}' if step else None)

    def step(self, n, m, text):
        self._ctx.progress(None, f'{self._phase}: {text}')

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        # run_backup sets .result on its task; keep the enable task's own result


def _step(ctx, phase, text, pct=None):
    ctx.progress(pct, f'{phase}: {text}')
    ctx.log(f'{phase}: {text}', stream='system')


def _check_ssh(res, what):
    facts = ms.parse(res.stdout)
    if res.returncode != 0 or facts.get('error'):
        raise tasks.TaskError(f"{what} failed: {facts.get('error') or res.stderr.strip()[-300:] or 'error'}")
    return facts


def _notify(job, message):
    m = _m()
    try:
        m.send_notification(job['id'], f"{job.get('name') or job['id']} (maintenance)", 'failed',
                            m.sanitize_error_message(message, 1000), 0)
    except Exception as e:
        logger.error(f"Maintenance alert for job {job.get('id')} failed: {e}")


def _secrets(ctx, job, db_config, creds=None):
    m = _m()
    try:
        ctx.add_secret(*m._job_secret_values(job, m.get_job_storage_targets(job)))
    except Exception:
        pass
    ctx.add_secret(db_config.get('password') or '', *credentials.secret_values(creds))


def _derive_in_task(ctx, job_id, params, only_current=False):
    facts = gather(job_id)
    items = params.get('items')
    if only_current:
        # The window task: no-downtime items applied earlier are gone from the plan by now
        job, server, db_config, kind = job_context(job_id)
        current = {i['id'] for i in _items(kind, job, _target(kind, db_config, server, facts), facts)
                   if i['auto']}
        items = [i for i in items or [] if i in current]
    plan, response = derive(job_id, facts, {'items': items, 'restart': params.get('restart'),
                                            'restart_at': params.get('restart_at'),
                                            'fresh_backup': params.get('fresh_backup')})
    return facts, plan, response


# ---------------------------------------------------------------------------
# Phase A: no-downtime items
# ---------------------------------------------------------------------------

def _client_image(job, db_config):
    if (db_config.get('docker_container') or '').strip():
        return ''
    return (job.get('pitr_client_image') or '').strip()


def _mysql_sql(server, db_config, job, statements, admin=None):
    res = _run(server, ms.mysql_sql_script(db_config, statements, _client_image(job, db_config), admin), 180)
    facts = ms.parse(res.stdout)
    if facts.get('connect_error'):
        raise tasks.TaskError('Connecting to the database failed: ' + facts['connect_error'])
    errors = ms.parse_lines(res.stdout, 'sql_error')
    if errors:
        raise tasks.TaskError('SQL failed: ' + errors[0].split(' ', 1)[-1])
    done = [x for x in ms.parse_lines(res.stdout, 'sql') if x.endswith(' ok')]
    if len(done) != len(statements):
        raise tasks.TaskError('SQL did not complete: ' + (res.stderr.strip()[-300:] or 'no output'))


def _write_mysql_dropin(ctx, server, target, facts, settings_, tag):
    existing = ms.unb64((facts.get('host') or {}).get('dropin_b64', ''))
    content = pe.dropin_content(existing, settings_)
    _check_ssh(_run(server, ms.write_file_script(tag, target['config_path'], content)), 'Writing the drop-in')
    ctx.log(f"Wrote {target['config_path']}: " + ', '.join(f'{k}={v}' for k, v in settings_.items()))


def _validate_mysql(ctx, server, target, settings_, tag):
    """Validate; on failure restore the drop-in and raise."""
    res = _run(server, ms.mysql_validate_script(target), 180)
    facts = ms.parse(res.stdout)
    problems = []
    if facts.get('valid') != '1':
        lines = ms.parse_lines(res.stdout, 'verr') or [res.stderr.strip()[-300:] or 'validation failed']
        # mysqld prints notes before the error: lead with the lines that say what's wrong
        problems = sorted(lines, key=lambda x: 0 if re.search(r'error|unknown|invalid', x, re.I) else 1)
    else:
        bad = pe.overridden_settings(pe.effective_options(ms.parse_lines(res.stdout, 'pd')), settings_)
        if bad:
            problems = [f'{n} is overridden by a later option file (effective: {v})' for n, v in bad]
    if problems:
        for line in problems[:20]:
            ctx.log('  ' + line, stream='stderr')
        _restore_file(ctx, server, tag, target['config_path'])
        raise tasks.TaskError('The configuration did not validate, so nothing was restarted and the change was '
                              'undone: ' + '; '.join(problems[:3]))
    ctx.log('Configuration valid')


def _restore_file(ctx, server, tag, path):
    res = _run(server, ms.restore_file_script(tag, path))
    facts = ms.parse(res.stdout)
    if facts.get('restored') or facts.get('removed'):
        ctx.log(f"Restored the previous {path}" if facts.get('restored') else f'Removed {path}', stream='system')
        return True
    ctx.log(f"Could not restore {path}: {facts.get('error') or res.stderr.strip()[-200:]}", stream='stderr')
    return False


def apply_item(ctx, item, job, server, db_config, kind, target, facts, admin, tag):
    from . import pitr_service
    m = _m()
    action = item['action']
    if action == 'walg_install':
        targets = m.get_job_storage_targets(job)
        if not targets:
            raise tasks.TaskError('The job has no active storage for WAL-G')
        prefix = job.get('backup_prefix') or job['id']
        info_fields = pitr_service.install_walg(server, targets[0], prefix)
        state = pitr_service.load_state(job['id'])
        info = pitr_service._json(state.get('info'), {})
        info.update(info_fields, walg_env_hash=pitr_service.walg_env_hash(targets[0], prefix))
        pitr_service.save_state(job['id'], kind='postgres', info=json.dumps(info))
        ctx.log(f"wal-g installed ({info_fields.get('walg_version')})")
    elif action == 'job_field':
        image = (item.get('settings') or {}).get('pitr_client_image')
        conn = m.get_db_connection()
        conn.execute('UPDATE jobs SET pitr_client_image = ? WHERE id = ?', (image, job['id']))
        conn.commit()
        job['pitr_client_image'] = image
        ctx.log(f"Client image set to {image}")
    elif action == 'client_charset':
        dropin_dir = target.get('config_path', '').rsplit('/', 1)[0] if target.get('config_path') else None
        dropin_dir = dropin_dir or pe.host_dropin_dir((facts.get('host') or {}).get('includedirs'),
                                                      (facts.get('host') or {}).get('dropin_dirs'))
        if not dropin_dir:
            raise tasks.TaskError('No conf.d directory to put the client options in')
        res = _run(server, ms.charset_fix_script(tag, item.get('files') or [], item.get('charset') or 'utf8mb4',
                                                 dropin_dir))
        out = ms.parse(res.stdout)
        if out.get('ok') != '1':
            raise tasks.TaskError('Moving default-character-set failed (restored): ' +
                                  (out.get('error') or res.stderr.strip()[-200:]))
        ctx.log(f"Moved default-character-set out of [client] in {', '.join(item.get('files') or [])}")
    elif action in ('grant', 'sql'):
        _mysql_sql(server, db_config, job, item['sql'], admin if item['needs_admin'] else None)
        for s in item['sql']:
            ctx.log(f'Ran: {s}')
    elif action == 'dropin_sql':
        _mysql_sql(server, db_config, job, item['sql'], admin if item['needs_admin'] else None)
        ctx.log(f"Ran: {item['sql'][0]}")
    elif action == 'alter_system_reload':
        _pg_set(ctx, server, target, facts, item['settings'], tag)
        _check_ssh(_run(server, ms.pg_sql_script(['SELECT pg_reload_conf()'])), 'pg_reload_conf')
        ctx.log('Reloaded the configuration (pg_reload_conf)')
    else:
        raise tasks.TaskError(f'Unknown action {action}')


def _pg_set(ctx, server, target, facts, settings_, tag):
    if target['config_method'] == 'alter_system':
        # The previous postgresql.auto.conf is in the facts (and the config backup) for a file restore
        stmts = [f"ALTER SYSTEM SET {k} = {pitr._sql_string(v)}" for k, v in settings_.items()]
        res = _run(server, ms.pg_sql_script(stmts))
        errors = ms.parse_lines(res.stdout, 'sql_error')
        if errors:
            raise tasks.TaskError('ALTER SYSTEM failed (nothing restarted): ' + errors[0].split(' ', 1)[-1])
        for s in stmts:
            ctx.log(f'Ran: {s}')
    elif target['config_method'] == 'conf_d':
        content = pe.pg_confd_content('', settings_)
        _check_ssh(_run(server, ms.write_file_script(tag, target['config_path'], content)), 'Writing the conf.d file')
        ctx.log(f"Wrote {target['config_path']}")
    else:
        raise tasks.TaskError('No way to change the PostgreSQL configuration')


def phase_apply(ctx, job_id, plan, facts, admin, tag):
    job, server, db_config, kind = job_context(job_id)
    job = dict(job, id=job_id)
    target = _target(kind, db_config, server, facts)
    items = sorted((i for i in plan['items'] if i['downtime'] == 'none'), key=lambda i: ORDER.get(i['action'], 99))
    full = {i['id']: i for i in _items(kind, job, target, facts, restart_selected=bool(plan['restart']))}
    dropin_settings = {}
    for n, item in enumerate(items, 1):
        spec = dict(full.get(item['id']) or {}, **item)
        _step(ctx, pe.PHASE_APPLY, f"{spec.get('title') or item['id']} ({n}/{len(items)})")
        apply_item(ctx, spec, job, server, db_config, kind, target, facts, admin, tag)
        if item['action'] == 'dropin_sql':
            dropin_settings.update(item.get('settings') or {})
    if dropin_settings and not plan['restart']:
        # No restart planned: persist the dynamic settings now (validated, applied at the next restart)
        _step(ctx, pe.PHASE_APPLY, f"writing {', '.join(dropin_settings)} to {target['config_path']}")
        _check_ssh(_run(server, ms.mysql_backup_config_script(tag, target)), 'Backing up the configuration')
        _write_mysql_dropin(ctx, server, target, facts, dropin_settings, tag)
        _validate_mysql(ctx, server, target, dropin_settings, tag)
    return len(items)


# ---------------------------------------------------------------------------
# Phase B: restart (maintenance flow)
# ---------------------------------------------------------------------------

def _status(server, kind, target, db_config, job, names, since, host):
    if kind == 'postgres':
        script = ms.pg_status_script(target['service'], names, since, host.get('log_file') or '')
    else:
        script = ms.mysql_status_script(target, db_config, _client_image(job, db_config), names, since,
                                        host.get('error_log') or '')
    res = _run(server, script, 60)
    facts = ms.parse(res.stdout)
    values = {k[4:]: v for k, v in facts.items() if k.startswith('var_')}
    restarting = facts.get('restarting') == 'true'
    started = _int(facts.get('started_at'))
    return {'state': 'restarting' if restarting else facts.get('state'), 'sub': facts.get('sub'),
            'fresh': (started >= since - 1) if started else True,
            'connected': facts.get('connected') == '1', 'values': values,
            'log': ms.parse_lines(res.stdout, 'log'), 'restarts': _int(facts.get('restart_count')),
            'error': facts.get('connect_error')}


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def wait_healthy(ctx, server, kind, target, db_config, job, wanted, since, host, label, baseline_restarts=0):
    """Poll until healthy / crashed / recovery timeout. Returns (decision, reason, status)."""
    cfg = pe.settings()
    names = sorted(wanted) if wanted else (['log_bin'] if kind == 'mysql' else ['wal_level'])
    started = time.monotonic()
    last_reason = None
    while True:
        elapsed = time.monotonic() - started
        try:
            status = _status(server, kind, target, db_config, job, names, since, host)
        except Exception as e:  # ssh hiccup while the server restarts: keep polling
            status = {'state': 'unknown', 'connected': False, 'values': {}, 'log': [], 'error': str(e)[:200]}
        status['restarts'] = max(0, (status.get('restarts') or 0) - baseline_restarts)
        if pitr_test_fault('verify') and wanted and status.get('connected'):
            status['values'] = {k: 'backupx-test-fault' for k in wanted}
        decision, reason = pe.health_decision(kind, status, wanted, elapsed, cfg)
        if reason != last_reason:
            ctx.log(f'{label}: {reason} ({int(elapsed)} s)', stream='system')
            last_reason = reason
        if decision != pe.WAIT:
            return decision, reason, status
        time.sleep(cfg['poll_interval'])


def pitr_test_fault(name):
    """Fault injection for the end-to-end rig only (BACKUPX_TEST_FAULT=verify)."""
    import os
    return (os.environ.get('BACKUPX_TEST_FAULT') or '') == name


def _restart(ctx, server, kind, target):
    script = ms.pg_restart_script(target['service']) if kind == 'postgres' else ms.mysql_restart_script(target)
    facts = _check_ssh(_run(server, script, 300), 'Restart')
    return _int(facts.get('since')) or int(time.time())


def _rollback(ctx, job, server, kind, target, db_config, restart, facts, tag, status):
    """Undo the config change and restart again. Returns True when the database is healthy again."""
    host = facts.get('host') or {}
    _step(ctx, pe.PHASE_RESTART, 'rolling back the configuration')
    settings_ = restart['settings']
    if kind == 'postgres' and target['config_method'] == 'alter_system':
        restored = False
        if status.get('connected'):
            old = _auto_conf_values(ms.unb64(host.get('auto_conf_b64')))
            stmts = [(f"ALTER SYSTEM SET {k} = {pitr._sql_string(old[k])}" if k in old else f'ALTER SYSTEM RESET {k}')
                     for k in settings_]
            res = _run(server, ms.pg_sql_script(stmts))
            restored = not ms.parse_lines(res.stdout, 'sql_error')
            for s in stmts:
                ctx.log(f"Ran: {s}" if restored else f'Failed: {s}', stream='system')
        if not restored:
            # Server down: put the saved postgresql.auto.conf back as a file
            auto = (host.get('data_directory') or '') + '/postgresql.auto.conf'
            res = _run(server, ms.write_file_script(tag + '-rollback', auto, ms.unb64(host.get('auto_conf_b64')),
                                                    mode='600', owner='postgres:postgres'))
            ctx.log(f'Restored {auto} from the copy taken before the change', stream='system')
    else:
        _restore_file(ctx, server, tag, target['config_path'])
    since = _restart(ctx, server, kind, target)
    decision, reason, _st = wait_healthy(ctx, server, kind, target, db_config, job, {}, since, host,
                                         'After rollback')
    return decision == pe.HEALTHY, reason


def _auto_conf_values(text):
    import re
    out = {}
    for raw in (text or '').splitlines():
        m = re.match(r"^\s*([a-z_]+)\s*=\s*'(.*)'\s*$", raw)
        if m:
            out[m.group(1)] = m.group(2).replace("''", "'")
    return out


def phase_restart(ctx, job_id, restart, facts, tag, fresh_policy):
    m = _m()
    job, server, db_config, kind = job_context(job_id)
    job = dict(job, id=job_id)
    target = _target(kind, db_config, server, facts)
    host = facts.get('host') or {}
    cfg = pe.settings()
    what = target.get('service') or target.get('container')
    # 1. fresh backup
    fresh = pe.fresh_backup_decision(job.get('last_success'), max_age=cfg['fresh_backup_max_age'],
                                     force=fresh_policy == 'always')
    if fresh == 'run':
        _step(ctx, pe.PHASE_RESTART, 'fresh backup of the job before the restart')
        ok, message = m.run_backup(job_id, trigger='pre-maintenance', task=PhaseTask(ctx, pe.PHASE_RESTART),
                                   tags=['automated', 'pre-maintenance'])
        ctx.log(message, stream='system')
        if not ok:
            raise tasks.TaskError(f'The backup before the restart failed, so nothing was changed: {message}')
    else:
        ctx.log(f"Fresh backup skipped: last successful backup at {job.get('last_success')} "
                f"(less than {cfg['fresh_backup_max_age'] // 60} min ago)", stream='system')
    # 2. config backup
    _step(ctx, pe.PHASE_RESTART, 'backing up the configuration')
    if kind == 'postgres':
        script = ms.pg_backup_config_script(tag, host.get('data_directory') or '', host.get('config_file') or '')
    else:
        script = ms.mysql_backup_config_script(tag, target)
    backup = _check_ssh(_run(server, script, 300), 'Backing up the configuration')
    ctx.log(f"Configuration saved to {backup.get('backup')}")
    # 3. change + 4. validate
    settings_ = restart['settings']
    _step(ctx, pe.PHASE_RESTART, 'changing ' + ', '.join(f'{k}={v}' for k, v in settings_.items()))
    if kind == 'postgres':
        _pg_set(ctx, server, target, facts, settings_, tag)
        _step(ctx, pe.PHASE_RESTART, 'validating the configuration')
        res = _run(server, ms.pg_validate_script(host.get('postgres_bin') or '', host.get('config_file') or '',
                                                 sorted(settings_)), 120)
        vf = ms.parse(res.stdout)
        problems = ms.parse_lines(res.stdout, 'verr') if vf.get('valid') == '0' else []
        for k, v in settings_.items():
            got = vf.get(f'c_{k}')
            if got is not None and got.strip().lower() != str(v).lower():
                problems.append(f'{k} would be {got} (overridden elsewhere)')
        if problems:
            for line in problems[:10]:
                ctx.log('  ' + line, stream='stderr')
            _rollback_pg_settings(ctx, server, target, host, settings_, tag)
            raise tasks.TaskError('The configuration did not validate, so nothing was restarted and the change '
                                  'was undone: ' + '; '.join(problems[:3]))
        ctx.log('Configuration valid (pg_file_settings, postgres -C)')
    else:
        _write_mysql_dropin(ctx, server, target, facts, settings_, tag)
        _step(ctx, pe.PHASE_RESTART, 'validating the configuration')
        _validate_mysql(ctx, server, target, settings_, tag)
    # 5. restart
    _step(ctx, pe.PHASE_RESTART, f'restarting {what}')
    baseline = _status(server, kind, target, db_config, job, [], int(time.time()), host).get('restarts') or 0
    since = _restart(ctx, server, kind, target)
    # 6. health check
    _step(ctx, pe.PHASE_RESTART, f'waiting for {what} to come back')
    decision, reason, status = wait_healthy(ctx, server, kind, target, db_config, job, settings_, since, host,
                                            'Health', baseline_restarts=baseline)
    if decision == pe.HEALTHY:
        ctx.log(f'{what} is back with ' + ', '.join(f'{k}={v}' for k, v in settings_.items()))
        return {'restarted': what, 'settings': settings_, 'fresh_backup': fresh}
    if decision == pe.RECOVERY_TIMEOUT:
        message = (f'{what} is still starting / in crash recovery after the maximum wait; backupx did not roll '
                   'back (that could interrupt recovery). Check the server now.')
        _notify(job, message)
        raise tasks.TaskError(message)
    for line in (status.get('log') or [])[-15:]:
        ctx.log('  ' + line, stream='stderr')
    ok, after = _rollback(ctx, job, server, kind, target, db_config, restart, facts, tag, status)
    if ok:
        message = (f'Restarting {what} with the new configuration failed ({reason}); the previous configuration '
                   f'was restored and {what} is running again. PITR was not switched on.')
        _notify(job, message)
        raise tasks.TaskRolledBack(message)
    message = (f'Restarting {what} with the new configuration failed ({reason}) and the rollback did not bring it '
               f'back ({after}). Manual attention needed now; the configuration backup is in '
               f'{pe.CONFIG_BACKUP_ROOT}/{tag}.')
    _notify(job, message)
    raise tasks.TaskError(message)


def _rollback_pg_settings(ctx, server, target, host, settings_, tag):
    if target['config_method'] == 'alter_system':
        old = _auto_conf_values(ms.unb64(host.get('auto_conf_b64')))
        stmts = [(f"ALTER SYSTEM SET {k} = {pitr._sql_string(old[k])}" if k in old else f'ALTER SYSTEM RESET {k}')
                 for k in settings_]
        _run(server, ms.pg_sql_script(stmts))
        for s in stmts:
            ctx.log(f'Ran: {s}', stream='system')
    else:
        _restore_file(ctx, server, tag, target['config_path'])


# ---------------------------------------------------------------------------
# Phase C: stream + anchor
# ---------------------------------------------------------------------------

def _tick(ctx, job_id, force=False):
    from . import pitr_service
    with ctx.lock(pitr_service.lock_key(job_id), wait=300, what='PITR shipping of this job'):
        try:
            return pitr_service._tick(job_id, force=force)
        except Exception as e:
            pitr_service._record_failure(job_id, f'{type(e).__name__}: {e}')
            raise tasks.TaskError(_m().sanitize_error_message(f'PITR tick failed: {e}'))


def _sleep(ctx, seconds):
    if ctx.cancel_event.wait(seconds):
        raise tasks.TaskCancelled()


def phase_enable(ctx, job_id):
    from . import pitr_service
    m = _m()
    job, server, db_config, kind = job_context(job_id)
    _step(ctx, pe.PHASE_STREAM, 'switching point-in-time recovery on')
    conn = m.get_db_connection()
    conn.execute('UPDATE jobs SET pitr_enabled = TRUE WHERE id = ?', (job_id,))
    conn.commit()
    m.refresh_schedules()
    if kind == 'mysql':
        _step(ctx, pe.PHASE_STREAM, 'installing and starting the binlog streamer')
        ok, message = _tick(ctx, job_id, force=True)
        ctx.log(message)
        # Warm-up: the first dump can only anchor a chain that knows the server's uuid
        for attempt in range(30):
            state = pitr_service.load_state(job_id)
            chains = pitr_service.load_chains(job_id)
            if state.get('server_uuid') and chains and chains[-1].get('server_uuid'):
                ctx.log(f"Streamer recorded the server ({state['server_uuid']}), chain {chains[-1]['chain_id']}")
                break
            _step(ctx, pe.PHASE_STREAM, 'waiting for the streamer to record the server')
            _sleep(ctx, 10)
            _tick(ctx, job_id)
        else:
            raise tasks.TaskError('The streamer did not record the server within 5 minutes; check '
                                  f"journalctl -u backupx-pitr-binlog@{job_id}")
        _step(ctx, pe.PHASE_ANCHOR, 'anchored full dump (records the binlog position)')
        started = _now()
        ok, message = m.run_backup(job_id, trigger='pitr-enable', task=PhaseTask(ctx, pe.PHASE_ANCHOR))
        ctx.log(message, stream='system')
        if not ok:
            raise tasks.TaskError(f'The anchored dump failed: {message}')
        anchors = [a for a in pitr_service.load_anchors(job_id)
                   if a['time'] and a['time'] >= started - timedelta(minutes=1)]
        if not anchors:
            state = pitr_service.load_state(job_id)
            raise tasks.TaskError('The dump has no binlog position: ' + (state.get('last_error') or 'unknown'))
        ctx.log(f"Anchor {anchors[-1]['binlog_file']}:{anchors[-1]['binlog_pos']}")
    else:
        _step(ctx, pe.PHASE_STREAM, 'installing wal-g and checking archiving')
        ok, message = _tick(ctx, job_id)
        ctx.log(message)
        info = pitr_service._json(pitr_service.load_state(job_id).get('info'), {})
        if not info.get('archiving'):
            raise tasks.TaskError('WAL archiving is not active yet (archive_mode / archive_command); '
                                  'run the setup again with those items')
        _step(ctx, pe.PHASE_ANCHOR, 'WAL-G base backup')
        ok, message = pitr_service.run_pg_base_backup(job_id)
        ctx.log(message, stream='system')
        if not ok:
            raise tasks.TaskError(f'The WAL-G base backup failed: {message}')
    _step(ctx, pe.PHASE_ANCHOR, 'waiting for the PITR state to be ok')
    deadline = time.monotonic() + pe.settings()['ok_wait_seconds']
    state = {}
    while True:
        ok, message = _tick(ctx, job_id)
        state = pitr_service.load_state(job_id)
        if state.get('state') == pitr.STATE_OK:
            break
        if time.monotonic() >= deadline:
            raise tasks.TaskError(f"PITR is on but its state is {state.get('state')}"
                                  + (f": {state.get('last_error')}" if state.get('last_error') else ''))
        ctx.log(f"PITR state {state.get('state')}; checking again in 15 s", stream='system')
        _sleep(ctx, 15)
    window = (pitr_service._iso(state.get('window_start')), pitr_service._iso(state.get('window_end')))
    ctx.log(f'PITR ok, restorable {window[0]} .. {window[1]}')
    return {'state': state.get('state'), 'window_start': window[0], 'window_end': window[1]}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _audit_done(ctx, job_id, name, status, error=None, details=None):
    _m()._audit('PITR_ENABLE', 'job', job_id, name, dict({'phase': 'finished', 'task': ctx.id}, **(details or {})),
                status=status, error=(error or '')[:300] or None, user=ctx.created_by, ip=ctx.ip)


@tasks.register(TASK_ENABLE, title='Enable PITR', tier=tasks.TIER_MAINTENANCE, cancellable=False)
def _task_enable(ctx):
    params = ctx.params
    job_id = params['job_id']
    admin = credentials.take(ctx.id) if params.get('uses_admin') else None
    job, server, db_config, kind = job_context(job_id)
    _secrets(ctx, dict(job, id=job_id), db_config, admin)
    if params.get('uses_admin') and not admin:
        raise tasks.TaskError('The one-time admin login is gone (backupx restarted before the task started); '
                              'start again')
    try:
        with ctx.lock(f'{job_id}:pitr-enable', wait=10, what='Another PITR setup of this job'):
            _step(ctx, pe.PHASE_APPLY, 're-checking the prerequisites')
            facts, plan, response = _derive_in_task(ctx, job_id, params)
            if security.plan_hash(plan) != params.get('plan_hash'):
                raise tasks.TaskError('The plan changed after it was confirmed; nothing was done. Review it again.')
            tag = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{ctx.id[:8]}"
            applied = phase_apply(ctx, job_id, plan, facts, admin, tag)
            admin = None  # the one-time login is not needed after this phase
            if applied:
                _step(ctx, pe.PHASE_APPLY, 're-running the prerequisite check')
                facts = gather(job_id)
                still = [c['check'] for c in facts['checks'] if c['status'] == 'fail' and not c.get('needs_restart')]
                if still:
                    ctx.log('Still failing (not fixed by a restart): ' + ', '.join(still), stream='stderr')
            restart = plan['restart']
            result = {'applied': applied, 'restart': None}
            if restart and restart['mode'] == 'at':
                at = pitr._utc(restart['at'])
                child, _ = tasks.enqueue(TASK_RESTART, {'job_id': job_id, 'restart_hash': security.plan_hash(restart),
                                                        'fresh_backup': plan['fresh_backup'], 'parent': ctx.id},
                                         title=f"PITR restart window: {plan['job_name']}", resource_type='job',
                                         resource_id=job_id, parent_id=ctx.id, tier=tasks.TIER_MAINTENANCE,
                                         idempotency_key=f'{TASK_RESTART}:{job_id}', created_by=ctx.created_by,
                                         ip=ctx.ip, not_before=at.replace(tzinfo=None))
                ctx.log(f'Restart scheduled for {at.isoformat()} (task {child["id"]})', stream='system')
                result.update(scheduled_restart_task_id=child['id'], scheduled_for=at.isoformat(),
                              message=f"No-downtime changes applied; the restart runs in the window at "
                                      f"{at.isoformat()}, then PITR is switched on")
                ctx.result = result
                _audit_done(ctx, job_id, plan['job_name'], 'success', details={'scheduled_for': at.isoformat()})
                return result
            if restart:
                result['restart'] = phase_restart(ctx, job_id, restart, facts, tag, plan['fresh_backup'])
            result.update(phase_enable(ctx, job_id))
            result['message'] = (f"PITR is on: restorable {result.get('window_start')} .. "
                                 f"{result.get('window_end')}")
            ctx.result = result
    except tasks.TaskRolledBack as e:
        _audit_done(ctx, job_id, job.get('name'), 'failure', str(e), {'rolled_back': True})
        raise
    except Exception as e:
        _audit_done(ctx, job_id, job.get('name'), 'failure', str(e))
        raise
    finally:
        admin = None
        credentials.discard(ctx.id)
    _audit_done(ctx, job_id, job.get('name'), 'success')
    return result


@tasks.register(TASK_RESTART, title='PITR restart window', tier=tasks.TIER_MAINTENANCE, cancellable=False)
def _task_restart(ctx):
    params = ctx.params
    job_id = params['job_id']
    job, server, db_config, kind = job_context(job_id)
    _secrets(ctx, dict(job, id=job_id), db_config)
    parent = tasks.get_task(params.get('parent')) if params.get('parent') else None
    parent_params = json.loads(parent['params']) if parent and parent.get('params') else {}
    try:
        with ctx.lock(f'{job_id}:pitr-enable', wait=60, what='Another PITR setup of this job'):
            _step(ctx, pe.PHASE_RESTART, 're-checking the prerequisites')
            facts, plan, _response = _derive_in_task(ctx, job_id, dict(parent_params, restart='now',
                                                                       restart_at=None), only_current=True)
            restart = plan['restart']
            result = {'restart': None}
            if restart:
                expected = dict(restart, mode='at', at=parent_params.get('restart_at'))
                expected['at'] = pitr._utc(expected['at']).replace(second=0, microsecond=0).isoformat() \
                    if expected['at'] else None
                if security.plan_hash(expected) != params.get('restart_hash'):
                    raise tasks.TaskError('The restart plan changed since it was confirmed (the server changed); '
                                          'nothing was done. Run the PITR setup again.')
                tag = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{ctx.id[:8]}"
                result['restart'] = phase_restart(ctx, job_id, restart, facts, tag, params.get('fresh_backup'))
            else:
                ctx.log('Nothing needs a restart any more (already applied)', stream='system')
            result.update(phase_enable(ctx, job_id))
            result['message'] = f"PITR is on: restorable {result.get('window_start')} .. {result.get('window_end')}"
            ctx.result = result
    except tasks.TaskRolledBack as e:
        _audit_done(ctx, job_id, job.get('name'), 'failure', str(e), {'rolled_back': True, 'window': True})
        raise
    except Exception as e:
        _audit_done(ctx, job_id, job.get('name'), 'failure', str(e), {'window': True})
        raise
    _audit_done(ctx, job_id, job.get('name'), 'success', details={'window': True})
    return result

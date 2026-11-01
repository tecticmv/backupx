# UX overhaul

Long-lived branch `feat/ux-overhaul`. Goal: an IT admin can see what's wrong,
fix it safely from the app, restore without a terminal and turn on PITR with a
wizard. Built in phases by separate agents on this branch; each phase updates
the *Status* section at the end.

| Phase | Scope | State |
|---|---|---|
| P0 | Foundation: task runner, task tray, per-storage results, CSRF, step-up, plan hashes, writable known_hosts, long ops as tasks | **done** |
| P1 | Health checks + fixes + dashboard | next |
| P2 | Restore wizard (scratch DB first, then gated replace-production) | later |
| P3 | Enable-PITR wizard | later |

## Decisions (from the owner, apply to every phase)

- Users: IT admins only. One admin user, no roles.
- Step-up: confirm and maintenance actions need the admin password re-entered
  within the last 5 minutes (`STEP_UP_TTL`, default 300 s).
- Fix tiers:
  - **safe**: one click (re-run failed storages, re-check, install restic, ...).
  - **confirm**: anything with downtime, config edits, DB restarts, removing a
    key, trusting a host key, replacing production, an anchored dump as a fix.
    Show the exact plan + expected downtime; the user types a phrase; step-up.
  - **maintenance**: confirm + backupx takes the DB through change → validate
    → health-check, with a backup first and automatic rollback on failure.
    backupx MAY restart databases, but only through this flow.
- Restores: always into a scratch DB on the backupx server (172.19.5.200,
  docker socket mounted) first. Replace-production is a second, gated step
  with an automatic pre-restore backup and Undo. Pre-restore snapshots kept
  30 days; scratch DBs live 24 h.
- Alerts: critical only, Telegram, deduplicated per issue.
- Progress: polling, not SSE.
- Health sweep: hourly + on-demand re-check (R2 charges per operation).
- Stale restic locks: auto-unlock after 30 min only when the lock comes from a
  backupx-managed host, no backupx advisory lock is held on the repository
  (`repo_in_use()` is false) and no running job shares the repository;
  otherwise a confirm-tier fix.
- DB config edits: conf.d drop-in or `ALTER SYSTEM`. Docker DBs without a
  bind-mounted conf dir get manual instructions; never recreate containers.
- DB grants: one-time admin credential prompt, never stored (and never in argv
  or task params).
- Out of scope: restore drills.

## P0 architecture (what later phases build on)

### Tasks (`app/tasks.py`)

Every operation that can outlive a request is a **task**: the route validates,
calls `_enqueue_task_response(kind, params, ...)` and returns 202 with the
task; a runner thread does the work. Gunicorn's 300 s request timeout no
longer matters.

Tables (`TASK_SCHEMA_SQL` in `app/db/postgres.py`, created/migrated under the
schema lock):

- `tasks`: id, kind, title, parent_id, resource_type/id, params (JSON text,
  redacted, no secrets), tier, status (`queued` / `running` / `succeeded` /
  `failed` / `cancelled` / `rolled_back`), progress, step, idempotency_key
  (partial unique index while queued/running), cancel_requested, created_by,
  ip, worker, created/started/finished/heartbeat timestamps, not_before,
  log_seq, result (JSON text), error.
- `task_logs`: (task_id, seq) → ts, stream (stdout/stderr/system), line.

Runner: every gunicorn worker runs a `TaskRunner` (`TASK_MAX_CONCURRENT`
threads, default 4, its own pool - not the APScheduler executors). It polls
every `TASK_POLL_INTERVAL` (2 s) and claims with `FOR UPDATE SKIP LOCKED`,
heartbeats every `TASK_HEARTBEAT` (10 s) and turns `cancel_requested` into an
in-process Event. A running task without a heartbeat for `TASK_STALE_AFTER`
(90 s) is failed with error `interrupted` (at start-up and every 30 s by every
runner). Finished tasks are purged after `TASK_KEEP_DAYS` (30); task files
(`/app/data/task-files`) after 24 h. `TASK_RUNNER_ENABLED=false` keeps an
instance from running tasks (tests set it).

Writing a handler:

```python
@tasks.register('my_fix', title='Fix the thing', tier=tasks.TIER_CONFIRM, cancellable=True)
def _task_my_fix(ctx):
    job = _task_job(ctx)                       # params['job_id'] -> job, or TaskError
    ctx.add_secret(*_job_secret_values(job))   # redact these values in log lines
    with ctx.lock(job_id, wait=600, what='the job'):        # advisory run lock, waits, cancellable
        with ctx.repo_locks([job_repo_url(job, t) for t in targets]):   # shared repo lock
            ctx.step(1, 3, 'Backing up config')            # progress 0%, step text
            proc = _ssh_popen(_build_ssh_cmd_for_server(server), script)
            rc = ctx.stream(proc, timeout=900)              # logs lines, kills on cancel/timeout
    if rc != 0:
        raise tasks.TaskError('what went wrong')            # -> failed, message shown as is
    return {'message': 'done', ...}                         # -> succeeded, result JSON
```

- Status: return → `succeeded`; `TaskError` → `failed` with its message;
  `TaskRolledBack` → `rolled_back` (maintenance flow: change failed, rollback
  worked); `TaskCancelled` / cancel → `cancelled`; anything else → `failed`
  with the exception text. `ctx.result` set before raising is kept.
- Scripts can report progress by printing `BACKUPX-STEP n/m text`
  (progress = (n-1)/m).
- Child tasks: pass `parent_id` to `tasks.enqueue` (P2: restore → scratch
  restore → replace production).
- Cancellable kinds only; `backup_run`, `init_repo`, `install_restic`,
  `pitr_ship` are not (killing restic mid-write leaves a stale lock).

API: `GET /api/tasks?active=1&recent=<min>&resource_type=&resource_id=`,
`GET /api/tasks/:id`, `GET /api/tasks/:id/logs?after=<seq>&limit=`,
`POST /api/tasks/:id/cancel`, `GET /api/tasks/:id/download` (result `file`).

### Locks

Same namespace/functions as before (`app/scheduler/locks.py`):
`job_run_lock(key)` for `<job>` (backup run) and `<job>:pitr` (PITR tick), plus
new `repo_locks(urls, exclusive=False)` keyed `repo:<sha256(url)[:16]>`.
Backups (scheduled and manual), restores, ZIPs and repository init hold it
**shared**; `repo_in_use(url)` tries it exclusively. P1's stale-lock fix: only
`restic unlock` when `repo_in_use()` is false (and hold the exclusive lock while
unlocking). Retention forget/prune don't take it yet (they run under the job
lock; P1 should add it if the unlock fix can race them).

### Per-storage run results

Every backup run (scheduled or not) gets a `run_id` (`BackupRun`, a
thread-local set by `run_backup`). `_finish_backup` and the timeout/error
paths write `run_results` (run_id, job_id, storage_id, storage_name, status
`success`/`failed`/`not_run`/`timeout`/`error`, exit_code, snapshot_id, error,
log_id, task_id, trigger, timestamps) and the redacted, 256 KB-capped
per-storage output in `run_logs`. `history.run_id` links the history row. Kept
`RUN_RESULTS_KEEP_DAYS` (90).

- `GET /api/jobs/:id/runs` → runs with storages, `status`
  (success/partial/failed) and `failed_storage_ids`.
- `GET /api/runs/:run_id`, `GET /api/runs/:run_id/log?storage_id=`.
- `POST /api/jobs/:id/run` with `{"storage_ids": [...]}` re-runs only those
  storages (a "Retry failed storages" button just posts `failed_storage_ids`).

### Long operations now tasks

| Route | Task kind | Notes |
|---|---|---|
| `POST /api/jobs/:id/run` | `backup_run` | idempotency `backup:<job>`; 409 if a run holds the job lock |
| `POST /api/jobs/:id/init` | `init_repo` | job lock + repo locks |
| `POST /api/servers/:id/install-restic` | `install_restic` | |
| `POST /api/jobs/:id/pitr/ship` | `pitr_ship` | takes `<job>:pitr` itself |
| `POST /api/jobs/:id/snapshots/:sid/restore` | `restore_files` | step-up; audited (queued/finished/cancelled) |
| `POST /api/jobs/:id/snapshots/:sid/zip` | `snapshot_zip` | replaces `GET .../download-zip`; ZIP built in `/app/data/task-files`, then `download_url` |

The single-file `download` is unchanged. `restore-db` (in-place restore into
the live database) is off unless `ALLOW_DIRECT_DB_RESTORE=true`, has no UI,
and when enabled needs step-up; P2's scratch-DB wizard replaces it.

### Security

- CSRF: on for every mutating route except login (`/api/auth/login`). The SPA
  gets the token from `GET /api/auth/csrf` (also in the login response) and
  sends `X-CSRFToken`; `frontend/src/lib/api.ts` patches `window.fetch` so all
  existing calls get it, and retries once after a `{"code": "csrf"}` 400.
  Tokens don't expire by time (`WTF_CSRF_TIME_LIMIT=None`), only with the
  session. Unauthenticated API calls get JSON 401 (was a 500).
- Step-up: `POST /api/auth/step-up {password}` (5/min, audited
  `STEP_UP`/`STEP_UP_FAILED`), `GET /api/auth/step-up` → `{valid,
  valid_until, ttl}`. Routes use `@requires_step_up` (after
  `@login_required`) → 403 `{"code": "step_up_required"}`. Login/logout clear
  it. Sessions are Flask signed cookies, so a captured old cookie could replay
  an older step-up within its 5 minutes - acceptable for a single-admin LAN app.
- Confirmed plans (`app/security.py`): the server derives the plan from
  current state (never from the request) and returns
  `present_plan(plan, confirm_text)` → `{plan, plan_hash, confirm_text}`.
  The execute call sends `{plan_hash, confirm}`; `check_confirmed(plan, data,
  confirm_text)` re-derives and returns a 409 `plan_changed` (with the new
  plan) or 400 `confirmation_mismatch` / `plan_hash_missing`, else None.
  `plan_hash` is HMAC-SHA256(SECRET_KEY) over canonical JSON. Put everything
  the user agrees to (steps, downtime estimate, target names, versions) in
  the plan.
- known_hosts: ssh uses `-o UserKnownHostsFile=/app/data/known_hosts`
  (`SSH_KNOWN_HOSTS_FILE`), still `StrictHostKeyChecking=accept-new`. At
  start-up lines from the mounted `~/.ssh/known_hosts` are merged in (add only).
  P1 host-key trust/replace edits this file (confirm tier).

### Frontend

- `src/lib/api.ts`: `installApiFetch()` (CSRF), `apiJson()` → throws
  `ApiError {status, code}`.
- `src/hooks/useTasks.tsx`: `TasksProvider` polls `/api/tasks?recent=60`
  (2.5 s while anything runs or the tray is open, else 15 s). `track(task,
  {autoDownload, onDone})` after starting a task → toast on finish, Download /
  Details actions. `openTray(taskId)`.
- `src/components/TaskTray.tsx`: header button with running count → Sheet with
  running + last-hour tasks, Progress, step, live log (ScrollArea, polled
  `logs?after=`), Cancel, Download.
- `src/hooks/useStepUp.tsx`: `ensureStepUp(reason)` before a confirm action,
  or `withStepUp(() => fetch(...), reason)` to retry after the password dialog.
- Dashboard activity: official shadcn `chart` (recharts) weekly stacked bars;
  the hand-rolled `ContributionGraph` is gone.
- shadcn rule: official components only, added with the CLI (`npx
  shadcn@latest add <name>`). In this repo the CLI writes `import { cn } from
  "cn"` and adds an npm package `cn`: change the import to `@/lib/utils` and
  `npm uninstall cn` after every add.

## Status

### Done (P0)

Everything above. Tests: `tests/test_tasks.py` (runner claim/SKIP LOCKED,
idempotency, cancel queued/running/uncancellable, interrupted, heartbeat,
advisory + repo locks, redaction, log cap, process streaming/kill, purge, task
API, CSRF, step-up, plan hash, per-storage results + rerun, restore/zip/init
as tasks, known_hosts) and `tests/test_secrets_argv.py` (file restore task:
secrets only on stdin, never in params/task_logs).

### Next (P1: health + fixes + dashboard)

- Health model: per job/server/storage/PITR checks with severity, a stable
  `issue_key` (dedup for Telegram alerts) and suggested fixes; table of
  check results; hourly sweep (one APScheduler entry on the leader, executor
  `maintenance` or a task) + "Re-check" button → task.
- Fix catalogue: each fix = task kind + tier + `plan()` function. Safe:
  re-run failed storages (`storage_ids`), install restic, init repo,
  stale-lock unlock (rules above). Confirm: host-key trust (edit
  `/app/data/known_hosts`), anchored dump, config edits, grants (one-time
  credential prompt; pass it to the task in memory, not in params - add a
  short-lived in-process secret handoff keyed by task id).
- Dashboard: health summary, failing items first, per-storage last run
  (`/api/jobs/:id/runs`), Retry-failed button.
- Critical-only Telegram alerts with per-issue dedup (reuse the PITR
  transition idea: alert on enter/leave).
- Consider adding repo locks to retention forget/prune.

### Later

- P2 restore wizard: scratch DB container on 172.19.5.200 (TTL 24 h), then
  replace-production (maintenance tier, pre-restore backup kept 30 days, Undo).
  Parent/child tasks.
- P3 enable-PITR wizard on top of `pitr_service.check_prereqs` + fixes.

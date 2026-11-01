# UX overhaul

Long-lived branch `feat/ux-overhaul`. Goal: an IT admin can see what's wrong,
fix it safely from the app, restore without a terminal and turn on PITR with a
wizard. Built in phases by separate agents on this branch; each phase updates
the *Status* section at the end.

| Phase | Scope | State |
|---|---|---|
| P0 | Foundation: task runner, task tray, per-storage results, CSRF, step-up, plan hashes, writable known_hosts, long ops as tasks | **done** |
| P1 | Health checks + fixes + dashboard | **done** |
| P2 | Restore wizard (scratch DB first, then gated replace-production) | **done** |
| P3 | Enable-PITR wizard, maintenance flow, auto client matching, restore drills | **done** |

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
- Restore drills: originally out of scope; added in P3 (weekly, checksum-verified, read-only on production).

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
unlocking). Retention forget runs inside the backup (shared lock held); prune
takes it shared since P1.

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
- Dashboard activity: P0 used a shadcn `chart` of weekly runs; P1 replaced it
  (activity timeline + storage usage chart); the hand-rolled `ContributionGraph` is gone.
- shadcn rule: official components only, added with the CLI (`npx
  shadcn@latest add <name>`). In this repo the CLI writes `import { cn } from
  "cn"` and adds an npm package `cn`: change the import to `@/lib/utils` and
  `npm uninstall cn` after every add.

## P1 architecture (health checks, fixes, dashboard)

### Model

- `app/health.py` (pure, no I/O): `Issue` (family, kind, severity
  `critical`/`warning`/`info`, title, plain explanation, resource, evidence,
  optional log excerpt, `fix` action or `manual` steps+commands, `alert`
  flag), `issue_id(family, *resource)` = sha256[:16] (stable across variants:
  `backup_failed` and `storage_unreachable` of one job share an id),
  `classify_error()` for restic/S3/SSH output, `lock_decision()` (stale-lock
  rule), the detectors, `alert_actions()`.
- `app/health_service.py`: state snapshot, findings, probes, sweep, alerts,
  the fix catalogue (`FIXES`) and the fix tasks; dashboard overview/activity.
- Tables (`TASK_SCHEMA_SQL`): `health_findings` (issue_id PK, family, kind,
  resource, title, severity, alert, first_seen, last_seen, resolved_at,
  snoozed_until, alerted_at, resolved_alerted_at, evidence) and
  `health_probes` (key PK, data JSON, checked_at): the cached results of the
  expensive checks `ssh:<server>`, `repo:<job>:<storage>`,
  `check:<job>:<storage>`, `stats:<job>:<storage>`, `prereq:<job>`.
  `notification_channels` got `last_error`, `last_error_at`,
  `last_success_at` (every send records its result, token redacted).
- `app_settings`: `scheduler_heartbeat` (the leader writes it on every lock
  tick), `backupx_hostnames` (container hostnames, for the stale-lock rule),
  `health_last_sweep`.

### Detectors

Cheap ones run on every `GET /api/health` from the database; probe-based
ones read `health_probes`.

| Kind | Source | Severity | Fix (tier) |
|---|---|---|---|
| `job_overdue` | overdue_since | critical | `run_now` (safe) |
| `backup_failed` | latest `run_results` per active storage | critical if no storage succeeded, else warning | `rerun_failed` (safe, only those storages) |
| `storage_unreachable` | same, every error a timeout/5xx/524/connection error | same | `storage_retry` (safe: endpoint test, retries with back-off `FIX_RETRY_BACKOFF`) |
| `repo_open_error` | run error or repo probe: wrong password | critical | manual |
| `retention_failed` / `retention_refused` / `prune_failed` | latest `retention_runs` | warning | `retry_forget` (safe: unlock-if-eligible + forget) / manual / manual |
| `pitr_streamer_down` | pitr_state: unit not active, heartbeat older than `PITR_HEARTBEAT_MAX_AGE`, or 3+ failed ticks | critical if lagging/broken | `pitr_kick` (safe: forced reinstall + restart, waits for a fresh heartbeat) |
| `wal_archive_failing` | pitr_state (PG) `failing` | critical if lagging | `walg_repush` (safe: re-install env/script, `wal-g st ls`) |
| `wal_archiving_off` | pitr_state (PG) `inactive` | warning | manual (postgresql.conf lines) |
| `wal_mirror_failed` | pitr_state info.mirror | warning | `wal_mirror_retry` (safe) |
| `pitr_broken` / `pitr_unanchored` | pitr_state | critical / warning | `anchored_dump` (confirm; prerequisites must pass, else manual) |
| `pitr_client_image` | prereq probe: binlog/dump client check fails or warns, docker present | warning | `pitr_client_image` (confirm: set the job field + forced kick) |
| `scheduler_down` | heartbeat older than max(120 s, 4 x lock interval + 30 s) | critical | manual |
| `notification_failing` | channel last_error_at > last_success_at | warning | `notification_test` (safe) |
| `ssh_unreachable` | SSH probe | critical if a scheduled/PITR job uses the server | `ssh_retest` (safe) |
| `host_key_changed` | SSH probe ("REMOTE HOST IDENTIFICATION HAS CHANGED") | critical | `trust_host_key` (confirm: old/new SHA256 fingerprints) |
| `restic_missing` | SSH probe | warning | `install_restic` (safe) |
| `low_disk` | SSH probe, `/`, `/var`, `/tmp` at 90 % or more | info | manual |
| `repo_not_initialized` | repo probe, restic exit 10 only | warning | `init_repo` (safe; the plan re-checks exit 10) |
| `repo_unreachable` | repo probe, any other open error | warning | `recheck_repo` (safe) |
| `stale_lock` / `stale_lock_foreign` | repo probe locks + `lock_decision` | warning | `unlock_stale` (safe) / `unlock_lock` (confirm) |
| `stray_key` | repo probe: a key that isn't the job's | warning | `remove_key` (confirm; phrase = key id) |
| `repo_check_failed` | weekly `restic check` (no read-data) | critical | `repair_index` (confirm) |

### Stale locks

`lock_decision(lock, now, managed_hosts, backupx_hosts, advisory_held,
repo_in_use)`: lock age under `STALE_LOCK_MIN_AGE` (1800 s) -> `wait` (never
touched); a backupx advisory lock on any job sharing the repository (`<job>`
= backup/prune, `<job>:pitr`, `<job>:walg-base`) or the repository lock held
-> `wait`; host = a managed server (address or probed `hostname`) or a
recorded backupx container hostname -> `auto` (safe fix); otherwise
`confirm` (the plan shows host, pid, age). Advisory locks are checked
read-only through `pg_locks`, never by taking them (that would make a
starting backup skip). The fix holds `repo_locks(exclusive=True)`, re-reads
the lock and decides again, then runs `restic unlock` (never `--remove-all`)
**on the lock's own host** over SSH when backupx manages it (restic there
also sees the pid is gone), else in the container, and lists the locks again
to verify. Prune now holds the repository lock shared too.

### Fixes

Each fix is a `Fix(action, tier, label, description, order)` with
`plan(issue)` (from current state; raises `NotApplicable`) and
`execute(ctx, plan, issue)`, registered as task kind `fix.<action>` (task
tier safe/confirm). Every fix task re-finds the issue (gone: succeeds with
`skipped`), re-derives the plan and compares the stored `plan_hash` (confirm
tier; a mismatch fails the task with nothing done), holds the advisory lock
`fix:job:<id>[:<storage>]` (or `fix:<type>:<id>`) so two fixes never work on
one resource at once, uses idempotency key `fix:<issue_id>`, writes audit
`HEALTH_FIX` (enqueue) and `HEALTH_FIX_DONE` (outcome) and re-syncs the
findings. Fix-all (`fix.all_safe`, idempotency `fix_all_safe`) re-selects
safe, unsnoozed issues among the previewed ids and runs them as child tasks
in waves by `order` (host key 4, SSH 5, unlock 10, install 20, init 30,
storage retry 45, rerun 50, run now 60, retention 70, PITR 80+,
notifications 90).

### Sweep and alerts

- Leader, executor `health` (1 thread): every `HEALTH_SWEEP_MINUTES` (60)
  queue the task `health_sweep` (idempotency `health_sweep:all`; any
  worker's runner runs it); every `HEALTH_ALERT_INTERVAL` (300 s)
  `alert_tick` (cheap detectors + findings sync + alerts).
- Sweep: SSH probe per server (login, hostname, restic, df); per job and
  storage `restic cat config`, `list locks` + `cat lock`, `key list` (all
  `--no-lock`, run in the container); `restic check` every
  `HEALTH_CHECK_DAYS` (7); `restic stats --mode raw-data` every
  `HEALTH_STATS_HOURS` (24); PITR prerequisites for MySQL/MariaDB jobs. Four
  threads. Drops probes of deleted resources, then syncs and alerts.
  `POST /api/health/recheck` runs it for one job/server/storage or all.
- Alerts: critical, `alert=True`, not snoozed, never alerted -> one message
  to the channels of type `HEALTH_ALERT_CHANNELS` (default `telegram`) with
  notify-on-failure on; when it clears -> one "Resolved" message. Claimed
  with `UPDATE ... WHERE alerted_at IS NULL RETURNING`, so each goes out once
  across workers. A resolved issue that comes back alerts again. Kinds that
  already notify elsewhere (backup failures, PITR state transitions,
  retention failures, channel failures) have `alert=False`, so no duplicate
  Telegram.

### API

- `GET /api/health?severity=&resource_type=&resource_id=&include_snoozed=1`
  -> `{summary: {status, total, critical, warning, info, snoozed,
  safe_fixable, last_sweep_at, sweep_running}, issues: [...]}` (syncs the
  findings: first_seen / last_seen / resolved).
- `POST /api/health/recheck {resource_type?, resource_id?}` -> 202 task.
- `POST /api/fixes {issue_id, preview?, idempotency_key?, plan_hash?,
  confirm?}`: preview -> `{plan, plan_hash, confirm_text?, fix}`; safe ->
  202 task; confirm -> step-up (403 `step_up_required`), then
  `check_confirmed` (400 `plan_hash_missing` / `confirmation_mismatch`, 409
  `plan_changed` with the new plan); also 409 `issue_resolved`, 409
  `not_applicable`, 400 `manual`.
- `POST /api/fixes/all-safe {preview: true}` -> `{count, fixes}`;
  `{issue_ids}` -> 202 parent task.
- `POST /api/issues/:id/snooze {hours: 0..720}` (0 = unsnooze).
- `GET /api/dashboard/overview[?job_id=]` -> job cards (latest result per
  storage, PITR window/lag/last_error, retention state, next run, issue
  counts), activity (history, retention_runs, tasks, PITR state changes,
  audit; 7 days), storage usage (from the `stats:` probes).

### Other changes

- Backup timeout: a watchdog kills a silent restic (restic retries a
  5xx/524 storage for about 15 min inside `cat config` without printing),
  and storages that finished before the deadline keep their result (only
  the unfinished ones become `timeout`; the error includes the last output).
- `pitr_service._tick(job_id, force=True)` and `ensure_and_scan_script(...,
  force=True)` reinstall and restart the streamer unconditionally;
  `check_prereqs` returns `docker`.
- `_dispatch_notification(channel, ...)` sends to one channel (raises).

### Frontend

- Dashboard: status bar (All good / N issues, k critical; last check;
  Re-check; "Fix all safe issues (n)" with a Popover preview), issues
  Accordion by severity (evidence HoverCard + log Collapsible; Fix, Review
  (AlertDialog: plan, downtime, details, typed phrase, step-up, plan_changed
  handling) or How to fix (steps, copyable commands, Re-check); snooze
  menu), job cards (ToggleGroup All/Issues/Running; per-storage chips, PITR,
  next run, retention; Run now, Restore placeholder, menu), activity
  timeline, storage usage chart (shadcn chart).
- `/jobs/:jobId` (`pages/JobDetail.tsx`), Tabs synced to `?tab=`: Overview
  (card + this job's issues), Runs (per-storage results, full logs, Retry
  failed storages), Snapshots (embedded), Recovery (P2 placeholder + PITR
  status), Settings (JobFormModal).
- New: `components/health/*`, `components/dashboard/*`,
  `components/jobs/JobRuns.tsx`, `hooks/useHealth.ts`, `lib/health.ts`,
  `lib/format.ts`, `types/health.ts`. Added shadcn components: accordion,
  alert-dialog, hover-card, popover, tabs, toggle, toggle-group.

## P2 architecture (restore wizard)

### Model

- `app/restore.py` (pure): settings, container names/labels, engine and
  scratch-image picking (from PITR's server version or the dump header:
  `-- Server version`, `-- Dumped from database version`), size estimates,
  the disk check, PITR windows/gaps and "latest restorable", key-table
  validation, the verify comparison, marker parsing, file-path validation.
- `app/restore_scripts.py` (shell text only): scratch MySQL/MariaDB/PostgreSQL
  start + load, the WAL-G scratch start, SQL runners (`bxsql` / `bximport` /
  `bxdump`) for the scratch container and for production over the job's own
  path (docker exec, direct, PITR client image), verification queries, the
  scratch dump for replace, the replace/rollback scripts run on the job's
  server, file restore (with pre-restore backup), helper-container extracts.
- `app/restore_service.py`: `restores` rows, plans, the task handlers, the
  cleanup tick. `pitr_service.mysql_restore_inputs()` is shared with the PITR
  restore script.
- Table `restores` (`TASK_SCHEMA_SQL`): id, parent_id (undo -> the restore it
  undoes), task_id, job, engine, point_type (`latest`/`snapshot`/`time`),
  snapshot_id, target_time, storage_id, target_type (`scratch`/`download`/
  `original`/`alternate`), server, container, work_dir (file extract dir),
  plan JSON, info JSON (server version, image, superuser, databases, host dir),
  pre_restore_snapshot JSON (`[{storage_id, storage_name, snapshot_id}]`),
  pre_restore_at, pre_restore_forgotten_at, verify JSON, cleanup_at, status,
  error, replace_task_id, undo_task_id. Statuses: running, ready, failed,
  cancelled, removed, expired, replacing, replaced, undoing, undone, done.
- `jobs.restore_verify` (JSON): `key_tables` (`db.table`, PG `db.schema.table`),
  `check_sql` (one read-only statement, run on prod and on the copy, output
  compared), `health_url` (GET after replace production).

### Where restores run

- Scratch restores run **in the backupx container** against the host's docker
  (the mounted socket): restic and wal-g run here, the scratch server is
  container `backupx-restore-<id>` with labels `backupx.restore=<id>`,
  `backupx.job=<job>`, **no published ports** (`--network none`; WAL-G
  scratch servers need the storage, so they join `RESTORE_DOCKER_NETWORK` or
  the default bridge). Files reach containers by `docker exec -i` / `docker
  cp` (tar streams), never bind mounts of container paths, so it works with
  any daemon. Work files: `RESTORE_WORK_DIR` (`/app/data/restores/<id>`),
  removed when the task ends.
- The image has the docker CLI (27.5.1, static, sha256 pinned) and wal-g
  v2.0.1 (ubuntu-18.04 build, also copied into the `postgres:<major>-bullseye`
  scratch server as `restore_command`). The backupx user needs the socket's
  group: `group_add: ${DOCKER_GID}` in docker-compose.yml (set `DOCKER_GID` to
  `stat -c %g /var/run/docker.sock` on the host).
- Secrets: restic password and S3 keys only in the subprocess env of the local
  `bash -s` / restic / wal-g; the WAL-G scratch server gets its S3 env as a
  0600 file inside its data directory (not in the container config); the
  production DB password only in the stdin script (0600 option file /
  exported PGPASSWORD; client-image mode passes the option file on stdin ahead
  of the data, up to a marker line). No secret in any argv, task param,
  result, log, restore row or audit entry (asserted end to end).

### Plans

`POST /api/restores/plan` derives from current state: the method (`dump` =
snapshot of a DB job, `binlog` = MySQL/MariaDB PITR, `walg` = PostgreSQL
PITR, `files`), the dump/base backup and binlog segments (via
`pitr.plan_restore`), sizes (snapshot `summary.total_bytes_processed`,
segment sizes, wal-g `uncompressed_size`), the scratch image, the steps, a
summary ("dump of ... + N binlog files, ~size to download") and a free-disk
check of `RESTORE_WORK_DIR`: needed = estimated restored size x
`RESTORE_DISK_FACTOR` (2.5; a gzipped dump counts x `RESTORE_DUMP_EXPANSION`
5). The plan hash covers everything except the free-space numbers.
`POST /api/restores` re-derives and refuses `plan_hash_missing` (400),
`plan_changed` (409 + new plan), `insufficient_disk` (400), `busy` (409, one
restore task per job). For `latest` the client echoes the planned
`target_time`; it is used while still restorable, so a binlog shipped in the
meantime doesn't change the plan. Exact-time downloads are the existing PITR
restore script; snapshot downloads are a `restore_download` task (restic dump
of the .sql.gz into task-files).

### Tasks

| Kind | Tier | Cancellable | What |
|---|---|---|---|
| `restore_scratch` | safe | yes | fetch + scratch container + load/replay (+ WAL replay and promote) + verify; failure/cancel removes container and work dir |
| `restore_download` | safe | yes | dump file of a snapshot -> download |
| `restore_dump` | safe | yes | dump the restored DB(s) from the scratch container -> download |
| `restore_verify` | safe | no | re-run the comparison |
| `restore_remove` | safe | no | docker rm -f by label, remove the extract dir |
| `restore_replace` | maintenance | no | see below |
| `restore_undo` | maintenance | no | scratch restore of the pre-restore snapshot + the replace flow |
| `restore_files_paths` | confirm | yes | file restore to original / alternate / extract on the backupx server |
| `restore_cleanup` | safe | no | leader every `RESTORE_CLEANUP_MINUTES` (10) |

Verification (`run_verify`): row estimates (`information_schema.tables` with
`information_schema_stats_expiry=0` on MySQL 8; PostgreSQL `pg_class.reltuples`
after `ANALYZE` on the copy), exact `COUNT(*)` of the key tables, the check
SQL; production is queried over the job's normal path (a failure there is
`prod_error`, the restore stays usable). The databases compared are the
configured ones, else all non-system ones.

### Replace production and Undo

`POST /api/restores/:id/replace-production {preview}` -> plan (production
target and path, source point, steps, downtime estimate, pre-restore and
rollback text, warnings) + `confirm_text` (the database name, or the DB
config's name for `*`); then `{plan_hash, confirm}` with step-up (403
`step_up_required`, 400 `confirmation_mismatch` / `plan_hash_missing`, 409
`plan_changed` / `not_ready`). Only a `ready` scratch DB restore that hasn't
expired qualifies. The task re-derives the plan and compares the stored hash
(changed -> nothing done), holds `<job>:restore`, then:

1. pre-restore backup: a real `run_backup(trigger='pre-restore', tags=
   ['pre-restore', 'restore:<id>'])`; it must succeed or nothing is touched.
   These snapshots don't get the `automated` tag, and retention forgets only
   `--tag automated`, so retention never removes them. The cleanup tick
   forgets exactly the recorded IDs after `PRE_RESTORE_KEEP_DAYS` (30), and
   only when `RETENTION_ENABLED` is on and not a dry run.
2. under the job's run lock: dump the restored databases from the scratch
   container (MySQL: `--databases ... --add-drop-database --routines
   --triggers --events --hex-blob --set-gtid-purged=OFF`, DEFINER clauses
   stripped unless the path is docker exec; MariaDB: no GTID flag; PostgreSQL:
   `pg_dump --clean --if-exists --create` per database);
3. copy it to the job's server (ssh with a fixed remote command, mktemp dir);
4. there: a rollback copy of production with the same flags, then the import
   (PostgreSQL: sessions on those databases terminated first; MySQL: `SET
   SESSION lock_wait_timeout=120` so a held lock fails instead of hanging);
   an import failure imports the rollback copy back -> task `rolled_back`;
5. verify again (prod vs scratch); a key-table mismatch (or production not
   queryable) imports the rollback copy -> `rolled_back`; the health URL
   result is logged;
6. status `replaced`, pre-restore snapshot recorded (shown with its expiry).

Undo (`/undo`, same two-phase gate, only for `replaced` with a kept
pre-restore snapshot) creates a child restore (`parent_id`) of the
pre-restore snapshot, runs the scratch restore and then the same replace flow
(including its own pre-restore backup, so an Undo can be undone).

### Files

- Browse: the existing lazy `GET .../snapshots/:sid/files?path=`; search:
  `GET /api/jobs/:id/snapshots/:sid/find?q=&storage=` (`restic find --json -i`,
  in the container, max `RESTORE_FIND_LIMIT` 500).
- `POST /api/restores/files/plan` / `POST /api/restores/files {job_id,
  snapshot_id, storage_id, paths, target, target_path?, plan_hash, confirm?}`:
  paths must be inside the snapshot's paths (not `/`). Targets: `original`
  (typed "overwrite" + step-up; first `restic backup <existing paths> --tag
  pre-restore --tag restore:<id>` on the host, its snapshot recorded, then
  `restic restore --target /` with `--include` per path), `alternate` (step-up;
  default `/var/tmp/backupx-restore/<job>-<ts>` on the job's server),
  `scratch` (restic restore here, then a `RESTORE_HELPER_IMAGE` container
  copies it to `RESTORE_HOST_FILES_DIR/<job>-<ts>` on the backupx server; 24 h
  TTL like DB scratch copies). ZIP download stays the `snapshot_zip` task.

### Cleanup

Leader job `__restore_cleanup__` (executor `health`) queues `restore_cleanup`:
rows `running` whose task is finished/missing (worker died -> task
`interrupted`) are cleaned and failed; `ready`/`replaced`/`undone` rows past
`cleanup_at` lose their scratch copy (`expired` for ready ones); containers
labelled `backupx.restore` without a live row are removed; orphan work dirs
are deleted; pre-restore snapshots past 30 days are forgotten (see above).

### API

`GET /api/jobs/:id/restore-points?storage=`, `GET/PUT
/api/jobs/:id/restore-verify`, `POST /api/restores/plan`, `POST
/api/restores`, `GET /api/restores?job_id=&active=1&limit=`, `GET
/api/restores/:id`, `POST /api/restores/:id/extend {hours}` (max 7 days from
now), `DELETE /api/restores/:id`, `POST /api/restores/:id/verify`, `POST
/api/restores/:id/dump`, `POST /api/restores/:id/replace-production`, `POST
/api/restores/:id/undo`, `GET /api/jobs/:id/snapshots/:sid/find`, `POST
/api/restores/files/plan`, `POST /api/restores/files`. Audit: RESTORE_START,
RESTORE_SCRATCH, RESTORE_REPLACE, RESTORE_UNDO, RESTORE_FILES,
RESTORE_REMOVE, RESTORE_EXTEND, RESTORE_VERIFY_CONFIG.

### Frontend

- `/restore` (`pages/Restore.tsx`), state in the URL (`?job=&snapshot=&storage=
  &restore=&step=`); opened from the job card's Restore button, the job page's
  Recovery tab (Start a restore + this job's restores), snapshot rows
  ("Restore..."; the old "Restore to server" dialog is gone) and the sidebar.
- Steps in a controlled shadcn `Tabs`: What (`Command` job search), When
  (storage `Select`, latest / snapshot table / exact time with `Calendar` +
  time input + `Slider` over the PITR windows, gaps shown, plan summary with
  the disk check), Where (scratch / download / replace production gated; file
  jobs: sidebar-11-style snapshot tree + `restic find` search, targets
  original / alternate / extract / ZIP), Run (progress, step, live log via
  `TaskLog`, Cancel), Verify (table, connect hint, Download dump, Re-run
  checks, Keep 24 h longer, Remove now, Replace production / Undo dialog,
  key-table settings).
- `components/restore/*`, `lib/restore.ts`, `types/restore.ts`. Added shadcn
  components: command, calendar, slider, radio-group, checkbox (npm: cmdk,
  react-day-picker, date-fns).

## P3 architecture (enable-PITR wizard, maintenance flow, drills)

Owner decisions for this phase: backupx MAY change database configuration and
restart databases, but only through the confirmed maintenance flow below
(exact plan + downtime estimate + typed database name + step-up + plan hash;
fresh backup first unless one is under 1 h old; config backup; validation;
restart; health check; automatic rollback, never while the server is still
recovering). Docker databases only when a conf dir is bind-mounted (never
recreate containers); managed databases: no config/restart options at all.

### Model

- `app/pitr_enable.py` (pure): the *target* (how the database runs: `host` /
  `docker` / `managed` / `remote`, systemd unit or container, config method
  `dropin` / `bind_mount` / `alter_system` / `conf_d` / `manual`, whether
  backupx may restart it and why not), remediation *items* from the
  prerequisite checks, the hashed plan, drop-in merging, `my_print_defaults`
  override detection and the post-restart `health_decision`.
- `app/maint_scripts.py` (shell text): host probes, SQL runner with a one-time
  admin option file (or `sudo mysql` over the socket), config backup (tar under
  `/var/lib/backupx-pitr/config-backups/<tag>/`), atomic file write/restore
  (previous version or an "absent" marker kept per run), validation, restart,
  status polls, the `[client]` charset fix, PostgreSQL equivalents.
- `app/pitr_enable_service.py`: facts (prerequisite check + probe, cached in
  `health_probes` `pitrplan:<job>` for 15 min), plan/apply/run state, the
  tasks `pitr_enable` and `pitr_enable_restart` (maintenance tier).
- `app/credentials.py` + `tasks.pinned_worker`: the one-time credential
  handoff (generic). The apply request carries the admin login; it is put in
  this process's memory keyed by the pre-generated task id and the task is
  pinned to this worker's runner (`claim_one` skips pinned tasks of other
  workers). The task takes (removes) it at start; it only reaches the database
  host on ssh stdin as a 0600 option file removed by a trap. Never in params,
  result, logs, audit or the database. A pinned task whose worker restarted is
  failed after `CREDENTIAL_TTL` (900 s) with "start again".

### Items (from failing checks)

| Check | Item | Action | Downtime |
|---|---|---|---|
| log_bin off | `mysql.log_bin` | drop-in `log_bin` (+ expiry if never purged: `expire_logs_days=2` on MariaDB < 10.6, else `binlog_expire_logs_seconds`) | restart |
| server_id 0 | `mysql.server_id` | drop-in | restart |
| binlog_format / row_image / short expiry | `mysql.binlog_*` | MySQL 8: `SET PERSIST`; MariaDB: `SET GLOBAL` + drop-in (folded into the restart drop-in when a restart happens anyway); docker w/o mount / remote: `SET GLOBAL` + manual config | none |
| privileges | `mysql.grants` | `GRANT REPLICATION SLAVE, REPLICATION CLIENT, RELOAD ... TO <CURRENT_USER()>` with the one-time login, or copy the SQL | none |
| client mismatch | `mysql.client_image` | automatic client matching (digest-pinned image) | none |
| mysqlbinlog rejects `[client] default-character-set` | `mysql.client_charset` | comment it out in `[client]` (backed up), set it for mysql/mysqldump/... in `zz-backupx-client-charset.cnf`; verified, restored on failure | none |
| archive_command / archive_timeout | `pg.archive_*` | `ALTER SYSTEM` + `pg_reload_conf()` | none |
| wal_level / archive_mode | `pg.wal_level` / `pg.archive_mode` | `ALTER SYSTEM` (conf.d drop-in only if `allow_alter_system=off`) | restart |
| wal-g not installed | `pg.walg_install` | existing installer | none |
| sudo / systemd / docker missing, connection, free space | manual | steps | - |

MySQL host installs write `zz-backupx-pitr.cnf` into the *last included*
conf.d directory (`/etc/mysql/mariadb.conf.d`, `mysql.conf.d`, `conf.d`,
`/etc/my.cnf.d`, from the `!includedir` chain). Before restarting, the
effective options (`my_print_defaults`) must show our values: a later file
that overrides them (e.g. `skip-log-bin`) blocks the restart. PostgreSQL:
`ALTER SYSTEM` writes `postgresql.auto.conf`, which is read last (a conf.d
drop-in would be shadowed by it, so conf.d is only the fallback when ALTER
SYSTEM is disabled).

### Flow

`POST /api/jobs/:id/pitr/plan` -> `POST /api/jobs/:id/pitr/apply` (step-up,
typed database-config name, plan hash; 400 `admin_required` / `stale_check`,
409 `plan_changed` / `busy`) -> task `pitr_enable`:

1. **Apply**: re-derive the plan from fresh facts (hash mismatch -> nothing
   done), the no-downtime items (wal-g, charset, client image, grants, SQL,
   MariaDB drop-in + validation when no restart follows, ALTER SYSTEM +
   reload), re-check.
2. **Restart** (now, or child task `pitr_enable_restart` with
   `tasks.not_before` = the window; its restart section is hashed on its own
   and re-derived at the window, so finished no-downtime items don't change
   it): fresh backup (`automated` + `pre-maintenance` tags) unless the last
   success is under `PITR_FRESH_BACKUP_MAX_AGE` (3600 s) or forced -> config
   backup -> write drop-in / ALTER SYSTEM -> validate (MySQL 8.0.16+
   `mysqld --validate-config`; MariaDB / older `mysqld --help --verbose`
   (exit 7 on unknown options) + `my_print_defaults`; PostgreSQL
   `pg_file_settings` errors (except "setting could not be applied" =
   pending restart) + `postgres -C`) -> on failure undo and stop ->
   `systemctl restart --no-block` / `docker restart` -> poll: the new instance
   (ExecMainStartTimestamp / StartedAt after the restart), connect, settings in
   effect. `crashed` (failed / auto-restart / exited, no recovery in the log)
   or `unhealthy` -> restore the config (MySQL file; PostgreSQL `ALTER SYSTEM
   RESET`/old value when it's up, else `postgresql.auto.conf` from the probed
   copy) -> restart -> healthy -> task `rolled_back` + alert; still bad ->
   failed + alert "manual attention". Recovery markers in the log (InnoDB crash
   recovery, "database system is starting up", ...) or `activating` past
   `PITR_MAINT_HEALTH_TIMEOUT` (300 s): keep waiting up to
   `PITR_MAINT_RECOVERY_MAX_WAIT` (7200 s), then fail **without** rollback +
   alert.
3. **Stream**: `pitr_enabled` on, forced tick (streamer / wal-g), warm-up
   until the chain has the server's uuid (fixes the P2 observation; chains
   created before the streamer recorded the server now also get the uuid).
4. **Anchor**: anchored dump (MySQL) / `wal-g backup-push` (PostgreSQL), then
   tick until the state is ok (`PITR_ENABLE_OK_WAIT` 600 s).

Steps are prefixed `Apply:` / `Restart:` / `Stream:` / `Anchor:`; `GET
/api/jobs/:id/pitr/enable` returns the newest run (parent + scheduled child,
phase, scheduled time) so the wizard resumes after a reload. Audit:
`PITR_ENABLE` (queued / finished).

### Automatic client-tool matching

`jobs.pitr_client_auto` (off for existing jobs; the wizard's client item turns
it on), `pitr_client_image_digest`, `pitr_client_server_version`. MySQL X.Y ->
`percona/percona-server:X.Y` (the official mysql images have no mysqlbinlog),
MariaDB X.Y -> `mariadb:X.Y`, unsupported lines -> nearest newer supported
line with a warning. The image is pulled on the job's server and pinned by
repo digest; the tick re-pins only when the server's major.minor changes. Any
server version change (auto or not) starts a new chain: earlier times stay
restorable, later ones need the next anchored dump (state `broken` until
then, the existing anchored-dump fix). `pitr.effective_client_image(job)` is
used by the streamer, prerequisite check, nightly dump and the restore
verify runner. Client containers run `--read-only --tmpfs /tmp --cap-drop
ALL --security-opt no-new-privileges`, no published ports, credentials on
stdin into the tmpfs. Inside images, `mysqldump`/`mysql` fall back to
`mariadb-dump`/`mariadb` (MariaDB 11). Not built: running the tools on the
backupx host when the job server has no docker (still host binaries or a
manual image there).

### Restore drills

`app/drill.py` (pure) + `app/drill_service.py`: task `restore_drill` (one
job, `POST /api/jobs/:id/restore-drill`, also the safe health fix
`restore_drill`) and `restore_drill_all` (leader cron `RESTORE_DRILL_CRON`,
default Saturday 03:30 scheduler TZ; one job at a time; `RESTORE_DRILL_ENABLED`
default true). Checksum production read-only (`CHECKSUM TABLE`; PostgreSQL md5
of the ordered row md5s; tables over `RESTORE_DRILL_MAX_TABLE_ROWS` 5 M by
count), wait until that second is shipped (`RESTORE_DRILL_SHIP_WAIT` 20 min),
scratch restore on the backupx server (P2 machinery), checksum the copy and
production again; every table that didn't change between the two production
reads must match exactly. Result: `health_probes` `drill:<job>`, a history
row, job card "Verified restore", job page drill card; failure -> critical
alert + issue `restore_drill_failed`.

### Health hooks

Issues carry `wizard: {kind: 'pitr_enable', job_id}`; IssueItem shows "Open
PITR setup". New kinds: `pitr_log_bin_off` / `pitr_prereq_failing` (from the
prerequisite probe of enabled jobs), `pitr_reload_missing`,
`restore_drill_failed`; `wal_archiving_off` and the manual variant of
`pitr_unanchored` link to the wizard.

### Frontend

`/jobs/:jobId/pitr-setup` (`pages/PitrSetup.tsx`, `components/pitr-setup/*`,
`lib/pitrSetup.ts`, `types/pitrSetup.ts`): controlled Tabs Check / Plan /
Apply / Restart / Stream / Anchor / Done (`?step=`), item checkboxes with
downtime and admin badges and copyable manual commands, admin login (socket /
password / run the SQL yourself; the password is cleared after submit),
restart now or in a window (Calendar + time, UTC shown), typed confirmation +
step-up, live task log, resume. Entry points: job card "Enable PITR" / menu,
issues, job page Recovery tab. `components/jobs/RestoreDrillCard.tsx`. No new
shadcn components were needed.

## Status

### Done (P0)

Everything above. Tests: `tests/test_tasks.py` (runner claim/SKIP LOCKED,
idempotency, cancel queued/running/uncancellable, interrupted, heartbeat,
advisory + repo locks, redaction, log cap, process streaming/kill, purge, task
API, CSRF, step-up, plan hash, per-storage results + rerun, restore/zip/init
as tasks, known_hosts) and `tests/test_secrets_argv.py` (file restore task:
secrets only on stdin, never in params/task_logs).

### Done (P1)

Everything in "P1 architecture". Tests: `tests/test_health.py` (every
detector on fixture states, stable ids, stale-lock rules incl. live lock,
advisory lock, repository in use and foreign host, alert dedup + resolved +
reopen, tier enforcement: fix-all never runs a confirm fix, confirm needs
preview + step-up + plan hash + phrase, the plan is re-derived at execution
(host key changed after confirming -> refused, known_hosts untouched),
idempotency, snooze, API, dashboard overview, sweep, probe classification
and redaction, SSH unlock keeps secrets out of argv, a backup timeout keeps
per-storage results).

E2E with throwaway containers (sshd target, 2 x MinIO, a small HTTP proxy
that answers 524 on demand, MySQL 8.4 + a systemd streamer host, Postgres):
repositories missing -> fix all (init); kill -9 restic mid-backup -> the
fresh lock is left alone -> fake clock +31 min -> stale lock is a safe fix
-> fix all (unlock on the lock's host, then retry only the failed storage)
-> next run ok; storage answers 524 -> `storage_unreachable` -> the retry
re-ran only that storage; streamer unit stopped -> `pitr_streamer_down` ->
restart fix -> active; SSH host key rotated -> confirm-tier trust with
old/new fingerprints. No secrets in task logs/params/results, probes,
findings or audit.

Not covered end to end: the PostgreSQL/WAL-G fixes, repair index, key
removal and client image change (plan/exec unit level only); the fix dialogs
were checked against a mock API, not clicked against the real backend.

### Done (P2)

Everything in "P2 architecture". Tests: `tests/test_restore.py` (disk check
and refusal, size estimates, PITR gaps / latest point, dump-header versions
and images, verify comparison and config validation, marker parsing, path
validation, extend cap, confirm text, no published ports, scratch dump flags,
prod runner keeps the password out of argv, file pre-restore ordering,
pre-restore runs not tagged `automated` + retention forgets only `automated`,
plan/start hash checks, busy, restore points, verify config API, replace
refused without ready scratch / step-up / typed text / hash, plan re-derived
in the task, pre-restore backup must succeed, key-table mismatch -> rolled
back with the rollback copy, success -> Undo plan, scratch failure and cancel
remove the container, TTL, cleanup of interrupted/expired/orphans,
pre-restore expiry only after 30 days with retention on, file restore gates,
local scripts get secrets only via env).

E2E with throwaway containers (rig in the P2 work dir: `bxp2-target` systemd +
sshd + docker CLI, MySQL 8.4 with GTID and the binlog streamer with the
percona client image, MariaDB 10.11 in docker-exec mode, Ubuntu 18.04 +
PostgreSQL 10 + WAL-G, MinIO, the backupx code in a container with the host
docker socket), 48 checks, all pass: MySQL PITR scratch restore to the exact
second (row at T-30 s present, row at T+10 s absent, row count = production
at T, trigger restored, no published ports), latest (newest row present),
snapshot (exactly the dump), verify (prod vs restored, key tables), dump
download, keep 24 h, remove now; disk pre-check refusal; cancel mid-restore
removes the container; replace refused without step-up (403) / typed text
(400) / ready scratch (409); replace production (production = restored point,
trigger kept, pre-restore snapshot tagged `pre-restore` without `automated`,
survives a retention forget with keep-daily 1); Undo (production back to the
pre-replace state); MariaDB docker-exec snapshot restore + replace (engine
corrected from the dump header); a worker killed mid-restore -> task
`interrupted` -> cleanup removes the container; PostgreSQL pg_dumpall
snapshot restore; **PostgreSQL WAL-G PITR to an exact time** (first end-to-end
run: row at T-30 s present, T+10 s absent); files: `restic find`, restore to
original (pre-restore snapshot holds the overwritten version), alternate path,
extract on the backupx host + remove; no secret in task params/results/logs,
restore rows or audit, and none in any process argv in the backupx container
(sampled every 100 ms).

Not covered end to end: replace production into PostgreSQL (unit level only),
DigitalOcean managed MySQL (GTID handling covered by the MySQL 8.4 GTID rig
via `--set-gtid-purged=OFF`; DEFINER stripping unit level), PostgreSQL 12+
WAL-G layout (`recovery.signal`, code path untested), the health URL check,
and the wizard UI was type-checked/linted/built, not clicked against the
real backend.

### Done (P3)

Everything in "P3 architecture". Tests: `tests/test_pitr_enable.py` (targets:
host / docker with and without conf mount / managed / remote; items and the
MariaDB fold-into-restart; MySQL 8 SET PERSIST; PostgreSQL reload vs restart;
plan and restart-section hashes; restart_at validation; fresh-backup rule;
drop-in merge and override detection; health decisions incl. never rolling
back during recovery; credentials taken once / expiring; admin password only
in the option file; pinned claims and orphan expiry; API gates (step-up,
typed name, hash, plan_changed, admin_required, busy); apply-now order and no
secret in params/logs/audit; validation failure and overridden option block
the restart; crash -> rollback + alert; recovery -> no rollback; scheduled
window runs only after not_before and refuses a changed restart plan; plan
changed after confirm -> nothing done; lost credential; PostgreSQL rollback via
ALTER SYSTEM RESET; every maintenance script passes `bash -n`),
`tests/test_prod_safety.py` (SQL allow-list for prerequisite / streamer /
status / probe / verify scripts, consistent dump flags, unique streamer server
ids, production imports only in replace/rollback, locked-down client
containers, auto image resolution, version change -> new chain, trusted-sources
hint), `tests/test_drill.py`.

E2E (rig `C:/Users/ali/claude-work/2026-11-01-backupx-p3/e2e`, label
`backupx-p3-rig=1`): MariaDB 10.3 host install (plan shows the restart; bogus
option elsewhere -> validation blocks, drop-in removed, not restarted; charset
fix; grants via the root socket; `log_bin` to a missing directory -> mysqld
fails -> rollback, MariaDB back, alert; apply now -> log_bin ON, streamer,
anchor, state ok); MySQL 8.4 (SET PERSIST + grants with a one-time `dba`
login; `skip-log-bin` in a later file -> override detected, no restart; after
fixing the file -> `mysqld --validate-config`, restart, ok); MariaDB 10.11 in
docker with a bind-mounted conf dir (docker restart, ok); docker without a
mount and a managed host: manual only; PostgreSQL 10 (reload item, forced
unhealthy -> rollback via ALTER SYSTEM RESET, scheduled window 2 min later ->
restart, base backup, ok). No admin password or other secret in tasks,
task_logs, audit, probes, the app log, any argv in the backupx container or
files left on the hosts. Version matrix: see `docs/PITR-VERIFICATION.md`.

Not covered end to end: the wizard UI clicked against the real backend
(type-checked, linted, built); PostgreSQL conf.d fallback (unit only); a
crash during real InnoDB recovery (decision unit-tested with log fixtures);
running client tools on the backupx host for servers without docker (not
built).

### Next

- Deploy: see `docs/DEPLOY-UX-OVERHAUL.md`.
- Health could raise "scratch restore left over" if the cleanup tick fails
  repeatedly; not built. Task tray history is 60 min.

### Later

- Client tools on the backupx host for job servers without docker.

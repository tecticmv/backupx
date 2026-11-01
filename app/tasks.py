"""
Background tasks: long operations that must outlive an HTTP request.

Anything that can take longer than a request may wait (gunicorn kills a
request after 300 s) runs as a task: the API inserts a row in `tasks` and
returns at once; a runner thread in some gunicorn worker claims it, runs the
registered handler and records progress, a step line, a redacted log
(`task_logs`) and a result. The SPA polls `/api/tasks` for progress (the task
tray).

Design notes
- Claiming: every gunicorn worker runs a TaskRunner. Workers poll every
  TASK_POLL_INTERVAL seconds and claim with `SELECT ... FOR UPDATE SKIP
  LOCKED`, so a task runs exactly once even with several workers/containers.
- Concurrency: at most TASK_MAX_CONCURRENT tasks per worker, on a thread pool
  of its own (never the APScheduler backup/pitr/maintenance executors).
- Locks: handlers take the existing advisory locks through TaskContext.lock()
  (job run lock "<job>", PITR lock "<job>:pitr") and the shared/exclusive
  repository locks (scheduler.locks.repo_locks).
- Liveness: the runner heartbeats its running tasks every TASK_HEARTBEAT
  seconds. A running task whose heartbeat is older than TASK_STALE_AFTER is
  marked failed "interrupted" (worker died, container restarted) at start-up
  and by every runner's periodic sweep.
- Cancel: a queued task is cancelled at once; a running one gets
  cancel_requested, which the heartbeat thread turns into an Event the handler
  sees (TaskContext.check_cancelled / stream kill the process).
- Idempotency: an optional idempotency_key is unique among *active* tasks
  (partial unique index); enqueueing the same key again returns the active
  task instead of starting a second one.
- Secrets: params and result are redacted with the audit logger's field
  rules before they are stored; every log line is redacted against the
  secret values the handler registered (ctx.add_secret) plus key=value
  patterns. Handlers must not put secrets in params at all; redaction is the
  safety net.
- Progress: a handler sets it directly (ctx.progress) or prints step markers
  `BACKUPX-STEP n/m text` from a script; ctx.log parses them.
"""

import json
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Statuses
QUEUED = 'queued'
RUNNING = 'running'
SUCCEEDED = 'succeeded'
FAILED = 'failed'
CANCELLED = 'cancelled'
ROLLED_BACK = 'rolled_back'
ACTIVE_STATUSES = (QUEUED, RUNNING)
FINAL_STATUSES = (SUCCEEDED, FAILED, CANCELLED, ROLLED_BACK)

# Tiers (see docs/UX-OVERHAUL.md): safe = one click; confirm = plan + typed
# confirmation + step-up; maintenance = confirm + downtime/rollback.
TIER_SAFE = 'safe'
TIER_CONFIRM = 'confirm'
TIER_MAINTENANCE = 'maintenance'
TIERS = (TIER_SAFE, TIER_CONFIRM, TIER_MAINTENANCE)

STEP_MARKER = re.compile(r'^\s*BACKUPX-STEP\s+(\d+)\s*/\s*(\d+)\s*(.*)$')


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def settings():
    return {
        'enabled': os.environ.get('TASK_RUNNER_ENABLED', 'true').lower() not in ('false', '0', 'no'),
        'max_concurrent': max(1, _env_int('TASK_MAX_CONCURRENT', 4)),
        'poll_interval': max(0.2, float(os.environ.get('TASK_POLL_INTERVAL', 2))),
        'heartbeat': max(1, _env_int('TASK_HEARTBEAT', 10)),
        'stale_after': max(10, _env_int('TASK_STALE_AFTER', 90)),
        'log_max_lines': max(100, _env_int('TASK_LOG_MAX_LINES', 20000)),
        'log_tail_lines': max(10, _env_int('TASK_LOG_TAIL_LINES', 500)),
        'line_max_chars': max(200, _env_int('TASK_LOG_LINE_MAX', 4000)),
        'keep_days': max(1, _env_int('TASK_KEEP_DAYS', 30)),
    }


def utcnow():
    """Naive UTC (the schema's TIMESTAMP columns hold UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id():
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Exceptions a handler may raise
# ---------------------------------------------------------------------------

class TaskError(Exception):
    """A handled failure: the message is shown to the user as is."""


class TaskCancelled(Exception):
    pass


class TaskRolledBack(Exception):
    """The operation failed and its automatic rollback succeeded."""


class TaskBusy(TaskError):
    """A lock the task needs is held by something else."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_KV_SECRET = re.compile(
    r'(?i)\b((?:[A-Z0-9_]*?)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)'
    r'[A-Z0-9_]*)(\s*[=:]\s*)("[^"]*"|\'[^\']*\'|[^\s"\']+)')
_URL_CREDS = re.compile(r'([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@', re.I)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secret values and key=value secrets in a line of output."""
    if not text:
        return text
    for value in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(value, '***')
    text = _KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + '***', text)
    text = _URL_CREDS.sub(lambda m: m.group(1) + m.group(2) + ':***@', text)
    return text


def redact_params(data: Any) -> Any:
    """Field-based redaction (the audit logger's rules), recursive through lists."""
    from .audit.logger import SENSITIVE_FIELDS
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if str(key).lower() in SENSITIVE_FIELDS:
                out[key] = '[REDACTED]' if value else value
            else:
                out[key] = redact_params(value)
        return out
    if isinstance(data, list):
        return [redact_params(v) for v in data]
    return data


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

class TaskKind:
    def __init__(self, kind, fn, title, tier, cancellable):
        self.kind = kind
        self.fn = fn
        self.title = title
        self.tier = tier
        self.cancellable = cancellable


_registry: Dict[str, TaskKind] = {}


def register(kind: str, title: str = '', tier: str = TIER_SAFE, cancellable: bool = True):
    """Decorator: `@tasks.register('restore_files', title='Restore files')` on fn(ctx)."""
    if tier not in TIERS:
        raise ValueError(f'unknown tier {tier}')

    def wrap(fn):
        _registry[kind] = TaskKind(kind, fn, title or kind, tier, cancellable)
        return fn
    return wrap


def get_kind(kind: str) -> Optional[TaskKind]:
    return _registry.get(kind)


# ---------------------------------------------------------------------------
# Store (rows in Postgres)
# ---------------------------------------------------------------------------

def _get_db():
    from .db.factory import get_database
    return get_database()


def _loads(text, default=None):
    if text is None or text == '':
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def serialize(row: Optional[dict]) -> Optional[dict]:
    """A task row as the API returns it."""
    if not row:
        return None
    kind = get_kind(row.get('kind'))
    return {
        'id': row['id'],
        'kind': row['kind'],
        'title': row.get('title') or (kind.title if kind else row['kind']),
        'parent_id': row.get('parent_id'),
        'resource_type': row.get('resource_type'),
        'resource_id': row.get('resource_id'),
        'params': _loads(row.get('params'), {}),
        'tier': row.get('tier'),
        'status': row['status'],
        'progress': row.get('progress') or 0,
        'step': row.get('step'),
        'cancel_requested': bool(row.get('cancel_requested')),
        'cancellable': bool(kind.cancellable) if kind else False,
        'created_by': row.get('created_by'),
        'created_at': _iso(row.get('created_at')),
        'started_at': _iso(row.get('started_at')),
        'finished_at': _iso(row.get('finished_at')),
        'heartbeat_at': _iso(row.get('heartbeat_at')),
        'result': _loads(row.get('result')),
        'error': row.get('error'),
        'log_seq': row.get('log_seq') or 0,
    }


def enqueue(kind: str, params: Optional[dict] = None, *, title: Optional[str] = None,
            resource_type: Optional[str] = None, resource_id: Optional[str] = None,
            parent_id: Optional[str] = None, tier: Optional[str] = None,
            idempotency_key: Optional[str] = None, created_by: Optional[str] = None,
            ip: Optional[str] = None, not_before: Optional[datetime] = None, db=None):
    """Queue a task. Returns (task_row, created).

    created is False when an active task with the same idempotency_key
    exists; that task is returned instead.
    """
    spec = get_kind(kind)
    if spec is None:
        raise ValueError(f'unknown task kind {kind}')
    db = db or _get_db()
    task_id = new_id()
    stored_params = json.dumps(redact_params(params or {}), default=str)
    cur = db.execute(
        '''INSERT INTO tasks (id, kind, title, parent_id, resource_type, resource_id, params, tier,
                              status, progress, idempotency_key, created_by, ip, created_at, not_before)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', 0, %s, %s, %s, %s, %s)
           ON CONFLICT (idempotency_key) WHERE status IN ('queued', 'running') AND idempotency_key IS NOT NULL
           DO NOTHING
           RETURNING *''',
        (task_id, kind, title or spec.title, parent_id, resource_type, resource_id, stored_params,
         tier or spec.tier, idempotency_key, created_by, ip, utcnow(), not_before))
    row = cur.fetchone()
    db.commit()
    if row:
        return dict(row), True
    existing = db.fetchone(
        "SELECT * FROM tasks WHERE idempotency_key = %s AND status IN ('queued', 'running')",
        (idempotency_key,))
    if existing:
        return existing, False
    # The active one finished between the INSERT and the SELECT: try once more
    return enqueue(kind, params, title=title, resource_type=resource_type, resource_id=resource_id,
                   parent_id=parent_id, tier=tier, idempotency_key=idempotency_key,
                   created_by=created_by, ip=ip, not_before=not_before, db=db)


def get_task(task_id: str, db=None) -> Optional[dict]:
    db = db or _get_db()
    return db.fetchone('SELECT * FROM tasks WHERE id = %s', (task_id,))


def list_tasks(active: bool = False, limit: int = 50, resource_type=None, resource_id=None,
               since: Optional[datetime] = None, db=None) -> List[dict]:
    db = db or _get_db()
    where, params = [], []
    if active:
        where.append("status IN ('queued', 'running')")
    if resource_type:
        where.append('resource_type = %s')
        params.append(resource_type)
    if resource_id:
        where.append('resource_id = %s')
        params.append(resource_id)
    if since is not None:
        where.append('(finished_at IS NULL OR finished_at >= %s)')
        params.append(since)
    sql = 'SELECT * FROM tasks'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY created_at DESC LIMIT %s'
    params.append(max(1, min(int(limit), 500)))
    return db.fetchall(sql, tuple(params))


def get_logs(task_id: str, after: int = 0, limit: int = 1000, db=None) -> List[dict]:
    db = db or _get_db()
    rows = db.fetchall(
        'SELECT seq, ts, stream, line FROM task_logs WHERE task_id = %s AND seq > %s ORDER BY seq LIMIT %s',
        (task_id, int(after), max(1, min(int(limit), 5000))))
    return [{'seq': r['seq'], 'ts': _iso(r['ts']), 'stream': r['stream'], 'line': r['line']} for r in rows]


def request_cancel(task_id: str, db=None):
    """Cancel a task. Returns (task_row, error); error is a message for a 4xx."""
    db = db or _get_db()
    row = get_task(task_id, db)
    if not row:
        return None, 'not_found'
    if row['status'] in FINAL_STATUSES:
        return row, 'Task already finished'
    if row['status'] == QUEUED:
        cur = db.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = %s, step = 'Cancelled before it started'"
            " WHERE id = %s AND status = 'queued' RETURNING *", (utcnow(), task_id))
        updated = cur.fetchone()
        db.commit()
        if updated:
            return dict(updated), None
        row = get_task(task_id, db)  # it was claimed meanwhile; fall through
    kind = get_kind(row['kind'])
    if kind is not None and not kind.cancellable:
        return row, 'This task cannot be cancelled once it has started'
    cur = db.execute("UPDATE tasks SET cancel_requested = TRUE WHERE id = %s AND status = 'running' RETURNING *",
                     (task_id,))
    updated = cur.fetchone()
    db.commit()
    if updated:
        _runner_cancel_local(task_id)
    return (dict(updated) if updated else get_task(task_id, db)), None


def mark_interrupted(stale_after: Optional[int] = None, db=None) -> int:
    """Fail running tasks whose heartbeat stopped (worker died). Returns how many."""
    db = db or _get_db()
    stale_after = stale_after if stale_after is not None else settings()['stale_after']
    cutoff = utcnow().timestamp() - stale_after
    cutoff_dt = datetime.fromtimestamp(cutoff, timezone.utc).replace(tzinfo=None)
    cur = db.execute(
        "UPDATE tasks SET status = 'failed', finished_at = %s, error = 'interrupted',"
        " step = 'Interrupted: the worker running it stopped (restart or crash)'"
        " WHERE status = 'running' AND COALESCE(heartbeat_at, started_at, created_at) < %s RETURNING id",
        (utcnow(), cutoff_dt))
    rows = cur.fetchall()
    db.commit()
    for r in rows:
        logger.warning(f"Task {r['id']} marked interrupted (no heartbeat for {stale_after}s)")
    return len(rows)


def purge_old(keep_days: Optional[int] = None, db=None) -> int:
    """Delete finished tasks (and their logs and files) older than keep_days."""
    db = db or _get_db()
    keep_days = keep_days or settings()['keep_days']
    cutoff = datetime.fromtimestamp(utcnow().timestamp() - keep_days * 86400, timezone.utc).replace(tzinfo=None)
    cur = db.execute(
        "DELETE FROM tasks WHERE status IN ('succeeded', 'failed', 'cancelled', 'rolled_back')"
        " AND COALESCE(finished_at, created_at) < %s RETURNING id", (cutoff,))
    ids = [r['id'] for r in cur.fetchall()]
    if ids:
        db.execute('DELETE FROM task_logs WHERE task_id = ANY(%s)', (ids,))
    db.commit()
    return len(ids)


# ---------------------------------------------------------------------------
# Task files (e.g. a generated ZIP the user downloads later)
# ---------------------------------------------------------------------------

def files_dir() -> str:
    base = os.environ.get('APP_DATA_DIR', '/app/data')
    path = os.path.join(base, 'task-files')
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def purge_files(max_age_seconds: int = 24 * 3600) -> int:
    """Delete task output files older than max_age_seconds."""
    try:
        path = files_dir()
    except OSError:
        return 0
    removed = 0
    now = time.time()
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            if now - os.path.getmtime(full) > max_age_seconds:
                if os.path.isdir(full):
                    import shutil
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.unlink(full)
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Context handed to handlers
# ---------------------------------------------------------------------------

class TaskContext:
    """What a handler gets: params, logging, progress, cancel, locks."""

    def __init__(self, row: dict, db=None, connect_lock: Optional[Callable] = None,
                 cancel_event: Optional[threading.Event] = None, cfg: Optional[dict] = None):
        self.row = row
        self.id = row['id']
        self.kind = row['kind']
        self.params = _loads(row.get('params'), {}) or {}
        self.created_by = row.get('created_by')
        self.ip = row.get('ip')
        self._db = db
        self._connect_lock = connect_lock
        self.cancel_event = cancel_event or threading.Event()
        self._cfg = cfg or settings()
        self._secrets = set()
        self._seq = int(row.get('log_seq') or 0)
        self._written = self._seq
        self._buffer: List[tuple] = []
        self._tail: List[tuple] = []
        self._dropped = 0
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._progress = int(row.get('progress') or 0)
        self._step = row.get('step')
        self.result: Optional[dict] = None
        self._locks: List[Any] = []

    @property
    def db(self):
        return self._db or _get_db()

    # -- secrets ---------------------------------------------------------
    def add_secret(self, *values):
        for v in values:
            if v and isinstance(v, str):
                self._secrets.add(v)

    def redact(self, text):
        return redact_text(text, self._secrets)

    # -- logging ---------------------------------------------------------
    def log(self, line, stream='stdout'):
        """Append one line (redacted, step markers parsed). Thread-safe."""
        if line is None:
            return
        text = str(line).rstrip('\n')
        for part in text.split('\n') if '\n' in text else [text]:
            self._log_one(part.rstrip('\r'), stream)

    def _log_one(self, text, stream):
        marker = STEP_MARKER.match(text)
        if marker:
            n, m, label = int(marker.group(1)), int(marker.group(2)), marker.group(3).strip()
            if m > 0:
                pct = int(max(0, min(n - 1, m)) * 100 / m)
                self.progress(max(self._progress, pct), label or self._step)
        text = self.redact(text)
        limit = self._cfg['line_max_chars']
        if len(text) > limit:
            text = text[:limit] + ' [line truncated]'
        with self._lock:
            entry = (utcnow(), stream, text)
            if self._seq >= self._cfg['log_max_lines']:
                # Over the cap: keep only the last lines, written at finish
                self._tail.append(entry)
                if len(self._tail) > self._cfg['log_tail_lines']:
                    self._tail.pop(0)
                    self._dropped += 1
                return
            self._seq += 1
            self._buffer.append((self._seq,) + entry)
            due = len(self._buffer) >= 50 or time.monotonic() - self._last_flush >= 1
        if due:
            self.flush()

    def flush(self, final=False):
        with self._lock:
            rows = self._buffer
            self._buffer = []
            if final and self._tail:
                if self._dropped:
                    self._seq += 1
                    rows.append((self._seq, utcnow(), 'system',
                                 f'[log truncated: {self._dropped} lines omitted]'))
                for entry in self._tail:
                    self._seq += 1
                    rows.append((self._seq,) + entry)
                self._tail = []
                self._dropped = 0
            self._last_flush = time.monotonic()
            seq = self._seq
        if not rows:
            return
        try:
            db = self.db
            db.executemany('INSERT INTO task_logs (task_id, seq, ts, stream, line) VALUES (%s, %s, %s, %s, %s)',
                           [(self.id, s, ts, st, ln) for s, ts, st, ln in rows])
            db.execute('UPDATE tasks SET log_seq = %s WHERE id = %s', (seq, self.id))
            db.commit()
        except Exception as e:
            logger.error(f"Task {self.id}: can't write log lines: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass

    # -- progress --------------------------------------------------------
    def progress(self, pct: Optional[int] = None, step: Optional[str] = None):
        if pct is not None:
            self._progress = max(0, min(100, int(pct)))
        if step is not None:
            self._step = self.redact(str(step))[:500]
        try:
            db = self.db
            db.execute('UPDATE tasks SET progress = %s, step = %s WHERE id = %s',
                       (self._progress, self._step, self.id))
            db.commit()
        except Exception as e:
            logger.debug(f"Task {self.id}: progress update failed: {e}")

    def step(self, n: int, m: int, text: str):
        """Same as printing `BACKUPX-STEP n/m text`."""
        self.log(f'BACKUPX-STEP {n}/{m} {text}', stream='system')

    # -- cancel ----------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise TaskCancelled()

    # -- locks -----------------------------------------------------------
    @contextmanager
    def lock(self, key: str, wait: float = 0, what: str = ''):
        """Hold the advisory run lock `key` (job id, "<job>:pitr", ...).

        wait > 0: poll every 5 s (cancellable) for up to `wait` seconds.
        Raises TaskBusy if it can't be had.
        """
        from .scheduler.locks import job_run_lock
        deadline = time.monotonic() + max(0, wait)
        announced = False
        while True:
            cm = job_run_lock(self._connect_lock, key)
            acquired = cm.__enter__()
            if acquired:
                try:
                    yield
                finally:
                    cm.__exit__(None, None, None)
                return
            cm.__exit__(None, None, None)
            if time.monotonic() >= deadline:
                raise TaskBusy(f'{what or key} is busy (another run holds its lock)')
            if not announced:
                self.log(f'Waiting for {what or key} to be free...', stream='system')
                announced = True
            if self.cancel_event.wait(5):
                raise TaskCancelled()

    @contextmanager
    def repo_locks(self, urls: List[str], exclusive: bool = False):
        """Mark restic repositories in use (shared) or take them exclusively."""
        from .scheduler.locks import repo_locks
        with repo_locks(self._connect_lock, urls, exclusive=exclusive) as acquired:
            if not acquired:
                raise TaskBusy('A repository this task needs is in exclusive use (unlock/prune)')
            yield

    # -- processes -------------------------------------------------------
    def stream(self, proc: subprocess.Popen, timeout: Optional[float] = None,
               on_line: Optional[Callable[[str, str], None]] = None) -> int:
        """Log a process's stdout/stderr line by line until it exits.

        Kills it when the task is cancelled (raises TaskCancelled) or after
        timeout seconds (raises subprocess.TimeoutExpired). Returns the exit code.
        """
        lines: 'queue.Queue' = queue.Queue()

        def pump(pipe, name):
            try:
                for raw in pipe:
                    lines.put((name, raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw))
            except (ValueError, OSError):
                pass
            finally:
                lines.put((name, None))

        pumps = []
        for pipe, name in ((proc.stdout, 'stdout'), (proc.stderr, 'stderr')):
            if pipe is not None:
                t = threading.Thread(target=pump, args=(pipe, name), daemon=True)
                t.start()
                pumps.append(t)
        open_pipes = len(pumps)
        deadline = time.monotonic() + timeout if timeout else None

        def kill():
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass

        while open_pipes:
            if self.cancel_event.is_set():
                kill()
                self.log('Cancelled: process killed', stream='system')
                raise TaskCancelled()
            if deadline and time.monotonic() > deadline:
                kill()
                raise subprocess.TimeoutExpired(cmd='task', timeout=timeout)
            try:
                name, line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                open_pipes -= 1
                continue
            line = line.rstrip('\n')
            self.log(line, stream=name)
            if on_line:
                on_line(name, line)
        remaining = (deadline - time.monotonic()) if deadline else None
        try:
            return proc.wait(timeout=max(1, remaining) if remaining else None)
        except subprocess.TimeoutExpired:
            kill()
            raise


def _finish(ctx: TaskContext, status: str, result=None, error=None, step=None):
    ctx.flush(final=True)
    progress = 100 if status == SUCCEEDED else ctx._progress
    db = ctx.db
    try:
        db.execute(
            'UPDATE tasks SET status = %s, finished_at = %s, result = %s, error = %s, progress = %s,'
            ' step = COALESCE(%s, step) WHERE id = %s AND status = %s',
            (status, utcnow(), json.dumps(redact_params(result), default=str) if result is not None else None,
             ctx.redact(error)[:2000] if error else None, progress,
             ctx.redact(step)[:500] if step else None, ctx.id, RUNNING))
        db.commit()
    except Exception as e:
        logger.error(f"Task {ctx.id}: can't record its outcome ({status}): {e}")


def execute(row: dict, connect_lock: Optional[Callable] = None, cancel_event=None, db=None) -> str:
    """Run one claimed task to completion. Never raises. Returns the final status."""
    db_backend = db or _get_db()
    scope = getattr(db_backend, 'short_lived_connections', None)
    if scope is None:
        return _execute(row, connect_lock, cancel_event, db)
    # Borrow a pooled connection per query, like backup runs: a task may run for hours
    with scope():
        return _execute(row, connect_lock, cancel_event, db)


def _execute(row, connect_lock, cancel_event, db):
    ctx = TaskContext(row, db=db, connect_lock=connect_lock, cancel_event=cancel_event)
    spec = get_kind(row['kind'])
    if spec is None:
        _finish(ctx, FAILED, error=f"Unknown task kind {row['kind']}")
        return FAILED
    try:
        result = spec.fn(ctx)
        if result is None:
            result = ctx.result
        if ctx.cancelled and spec.cancellable:
            raise TaskCancelled()
        _finish(ctx, SUCCEEDED, result=result, step='Done')
        return SUCCEEDED
    except TaskCancelled:
        _finish(ctx, CANCELLED, result=ctx.result, step='Cancelled')
        return CANCELLED
    except TaskRolledBack as e:
        ctx.log(f'Rolled back: {e}', stream='system')
        _finish(ctx, ROLLED_BACK, result=ctx.result, error=str(e), step='Rolled back')
        return ROLLED_BACK
    except TaskError as e:
        ctx.log(f'Failed: {e}', stream='system')
        _finish(ctx, FAILED, result=ctx.result, error=str(e), step='Failed')
        return FAILED
    except subprocess.TimeoutExpired as e:
        ctx.log(f'Timed out after {e.timeout}s', stream='system')
        _finish(ctx, FAILED, result=ctx.result, error=f'Timed out after {e.timeout}s', step='Timed out')
        return FAILED
    except Exception as e:
        logger.exception(f"Task {ctx.id} ({row['kind']}) crashed")
        msg = f'{type(e).__name__}: {e}'
        ctx.log(f'Failed: {msg}', stream='system')
        _finish(ctx, FAILED, result=ctx.result, error=msg, step='Failed')
        return FAILED
    finally:
        try:
            ctx.db.close()  # give this thread's pooled connection back
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_runner: Optional['TaskRunner'] = None


def _runner_cancel_local(task_id):
    if _runner is not None:
        _runner.cancel_local(task_id)


def claim_one(worker: str, db=None) -> Optional[dict]:
    """Claim the oldest runnable queued task, or None."""
    db = db or _get_db()
    now = utcnow()
    cur = db.execute(
        '''UPDATE tasks SET status = 'running', started_at = %s, heartbeat_at = %s, worker = %s
           WHERE id = (
               SELECT id FROM tasks
               WHERE status = 'queued' AND (not_before IS NULL OR not_before <= %s)
               ORDER BY created_at
               LIMIT 1
               FOR UPDATE SKIP LOCKED)
           AND status = 'queued'
           RETURNING *''', (now, now, worker, now))
    row = cur.fetchone()
    db.commit()
    return dict(row) if row else None


class TaskRunner:
    """Per-process poller + bounded pool. One per gunicorn worker."""

    def __init__(self, connect_lock: Callable, max_workers: Optional[int] = None,
                 poll_interval: Optional[float] = None, worker_id: Optional[str] = None):
        cfg = settings()
        self.cfg = cfg
        self.connect_lock = connect_lock
        self.max_workers = max_workers or cfg['max_concurrent']
        self.poll_interval = poll_interval or cfg['poll_interval']
        self.worker_id = worker_id or f'{socket.gethostname()}:{os.getpid()}:{new_id()[:4]}'
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix='task')
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._running: Dict[str, threading.Event] = {}
        self._running_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._last_sweep = 0.0
        self._last_purge = 0.0

    # lifecycle
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for target, name in ((self._poll_loop, 'task-poller'), (self._heartbeat_loop, 'task-heartbeat')):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        logger.info(f"Task runner started ({self.worker_id}, {self.max_workers} slots)")

    def stop(self, wait=False):
        self._stop.set()
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def running_ids(self):
        with self._running_lock:
            return list(self._running)

    def cancel_local(self, task_id):
        with self._running_lock:
            ev = self._running.get(task_id)
        if ev:
            ev.set()

    # one poll round (also used by tests)
    def poll_once(self) -> int:
        """Claim and start as many tasks as there are free slots. Returns how many started."""
        started = 0
        while not self._stop.is_set() and self._slots.acquire(blocking=False):
            try:
                row = claim_one(self.worker_id)
            except Exception as e:
                self._slots.release()
                logger.warning(f"Task claim failed: {e}")
                break
            finally:
                try:
                    _get_db().close()
                except Exception:
                    pass
            if not row:
                self._slots.release()
                break
            ev = threading.Event()
            with self._running_lock:
                self._running[row['id']] = ev
            self._pool.submit(self._run, row, ev)
            started += 1
        return started

    def run_once(self) -> Optional[str]:
        """Claim one task and run it in this thread (tests, CLI). Returns its status."""
        row = claim_one(self.worker_id)
        if not row:
            return None
        ev = threading.Event()
        with self._running_lock:
            self._running[row['id']] = ev
        try:
            return execute(row, connect_lock=self.connect_lock, cancel_event=ev)
        finally:
            with self._running_lock:
                self._running.pop(row['id'], None)

    def _run(self, row, ev):
        try:
            execute(row, connect_lock=self.connect_lock, cancel_event=ev)
        finally:
            with self._running_lock:
                self._running.pop(row['id'], None)
            self._slots.release()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self.maintenance()
                self.poll_once()
            except Exception as e:
                logger.warning(f"Task poll failed: {e}")
            self._stop.wait(self.poll_interval)

    def maintenance(self):
        now = time.monotonic()
        if now - self._last_sweep >= 30:
            self._last_sweep = now
            try:
                mark_interrupted(self.cfg['stale_after'])
            except Exception as e:
                logger.debug(f"Interrupted-task sweep failed: {e}")
        if now - self._last_purge >= 3600:
            self._last_purge = now
            try:
                purge_old(self.cfg['keep_days'])
                purge_files()
            except Exception as e:
                logger.debug(f"Task purge failed: {e}")
        try:
            _get_db().close()
        except Exception:
            pass

    def heartbeat(self):
        """Touch our running tasks and pick up cancel requests."""
        ids = self.running_ids()
        if not ids:
            return
        db = _get_db()
        try:
            cur = db.execute('UPDATE tasks SET heartbeat_at = %s WHERE id = ANY(%s) RETURNING id, cancel_requested',
                             (utcnow(), ids))
            rows = cur.fetchall()
            db.commit()
            for r in rows:
                if r['cancel_requested']:
                    self.cancel_local(r['id'])
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _heartbeat_loop(self):
        while not self._stop.wait(self.cfg['heartbeat']):
            try:
                self.heartbeat()
            except Exception as e:
                logger.warning(f"Task heartbeat failed: {e}")


def start_runner(connect_lock: Callable) -> Optional[TaskRunner]:
    """Mark interrupted tasks and start this process's runner (once)."""
    global _runner
    cfg = settings()
    try:
        mark_interrupted(cfg['stale_after'])
    except Exception as e:
        logger.warning(f"Could not sweep interrupted tasks: {e}")
    finally:
        try:
            _get_db().close()
        except Exception:
            pass
    if not cfg['enabled']:
        logger.info("Task runner disabled in this instance (TASK_RUNNER_ENABLED=false)")
        return None
    if _runner is None:
        _runner = TaskRunner(connect_lock)
        _runner.start()
    return _runner


def get_runner() -> Optional[TaskRunner]:
    return _runner

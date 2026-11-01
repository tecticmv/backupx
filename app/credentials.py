"""
One-time credentials for a single task (e.g. a database admin login for GRANTs).

The user types the credential into the request that starts the task. It is
kept only in this process's memory, keyed by the task id, and taken (removed)
by the task when it starts; it never goes into the task's params, its result,
the task log, the audit log or the database. Because it lives in one gunicorn
worker, the task is pinned to that worker's runner (tasks.pinned_worker): no
other worker can claim it. If the worker restarts first, the credential is
gone and the queued task is failed after CREDENTIAL_TTL with a message to
start again (tasks.fail_orphaned_pinned).

Inside the task the credential reaches the database host only on ssh's stdin,
as a 0600 option file removed by a trap (see app.maint_scripts).
"""

import os
import threading
import time
from typing import Optional

_lock = threading.Lock()
_store = {}  # task_id -> (expires_at, dict)


def ttl() -> int:
    try:
        return max(60, int(os.environ.get('CREDENTIAL_TTL', 900)))
    except (TypeError, ValueError):
        return 900


def _purge(now):
    for key in [k for k, (exp, _v) in _store.items() if exp <= now]:
        _store.pop(key, None)


def put(task_id: str, creds: dict, seconds: Optional[int] = None):
    """Hold creds for task_id (replaces an older entry)."""
    now = time.monotonic()
    with _lock:
        _purge(now)
        _store[task_id] = (now + (seconds or ttl()), dict(creds))


def take(task_id: str) -> Optional[dict]:
    """Return and forget the creds for task_id (None if missing or expired)."""
    now = time.monotonic()
    with _lock:
        _purge(now)
        entry = _store.pop(task_id, None)
    return dict(entry[1]) if entry else None


def has(task_id: str) -> bool:
    now = time.monotonic()
    with _lock:
        _purge(now)
        return task_id in _store


def discard(task_id: str):
    with _lock:
        _store.pop(task_id, None)


def secret_values(creds: Optional[dict]):
    """Values to register with ctx.add_secret / redaction."""
    if not creds:
        return []
    return [v for k, v in creds.items() if k in ('password', 'secret') and v]


def validate_admin(data) -> tuple:
    """Admin credential from a request body -> (creds or None, error or None).

    {"mode": "socket"}: the server's root over the local socket (no password).
    {"mode": "password", "username", "password"}.
    """
    if not data:
        return None, None
    if not isinstance(data, dict):
        return None, 'admin must be an object'
    mode = data.get('mode')
    if mode == 'socket':
        return {'mode': 'socket'}, None
    if mode == 'password':
        user = str(data.get('username') or '').strip()
        password = str(data.get('password') or '')
        if not user or not password:
            return None, 'Admin username and password are required'
        if len(user) > 80 or '\n' in user or '\n' in password or len(password) > 500:
            return None, 'Invalid admin credential'
        return {'mode': 'password', 'username': user, 'password': password}, None
    return None, 'admin.mode must be "socket" or "password"'

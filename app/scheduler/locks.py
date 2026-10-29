"""
Postgres advisory locks that keep scheduled backups single-run.

Gunicorn runs several workers (and there may be several containers), and each
one imports the app. These locks make sure that only one of them runs the cron
scheduler, and that a backup job never runs twice at the same time.

Advisory locks belong to a database session, so every lock is taken on its own
dedicated connection (never a pooled one): if the process holding it dies,
Postgres drops the connection and the lock is released.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# First key of the two-key advisory lock form; keeps BackupX locks apart from
# any other advisory locks on the same database.
SCHEDULER_LOCK_NAMESPACE = 0x42585301
JOB_LOCK_NAMESPACE = 0x42585302


@contextmanager
def job_run_lock(connect: Callable, job_id: str):
    """Hold the run lock for one backup job.

    Yields True if this caller got the lock, False if another run (in any
    worker or container) holds it. The lock is released on exit.
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS acquired',
                        (JOB_LOCK_NAMESPACE, job_id))
            row = cur.fetchone()
        yield bool(row['acquired'] if isinstance(row, dict) else row[0])
    finally:
        # Closing the session releases the lock
        conn.close()


def job_is_running(connect: Callable, job_id: str) -> bool:
    """True if some worker currently holds the run lock for this job."""
    with job_run_lock(connect, job_id) as acquired:
        return not acquired


class SchedulerLeader:
    """Elects the one worker that runs the scheduler.

    Every worker runs this loop. Each check, a worker that isn't the leader
    tries to take the scheduler lock; the leader confirms it still holds the
    lock and calls on_tick. If the leader dies its connection drops, the lock
    is released, and another worker takes over on its next check.
    """

    def __init__(self, connect: Callable, on_acquire: Callable, on_lose: Callable,
                 on_tick: Optional[Callable] = None, interval: float = 15, key: int = 0):
        self._connect = connect
        self._key = key
        self._on_acquire = on_acquire
        self._on_lose = on_lose
        self._on_tick = on_tick
        self._interval = interval
        self._conn = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.is_leader = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='scheduler-leader', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.is_leader:
            self._step_down()
        else:
            self._close()

    def _loop(self):
        while not self._stop.is_set():
            self.check()
            self._stop.wait(self._interval)

    def check(self):
        """One election round: take the lock if free, or confirm we still hold it."""
        try:
            if self.is_leader:
                if not self._holds_lock():
                    logger.warning("Scheduler lock lost; stopping the scheduler in this worker")
                    self._step_down()
                    return
                if self._on_tick:
                    try:
                        self._on_tick()
                    except Exception as e:
                        # Keep the lock and the running scheduler; retry next check
                        logger.error(f"Scheduler refresh failed: {e}")
            elif self._try_lock():
                self.is_leader = True
                logger.info("Took the scheduler lock; this worker now runs scheduled jobs")
                self._on_acquire()
        except Exception as e:
            logger.warning(f"Scheduler lock check failed: {e}")
            if self.is_leader:
                self._step_down()
            else:
                self._close()

    def _query_flag(self, sql):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        with self._conn.cursor() as cur:
            cur.execute(sql, (SCHEDULER_LOCK_NAMESPACE, self._key))
            row = cur.fetchone()
        return bool(row['flag'] if isinstance(row, dict) else row[0])

    def _try_lock(self):
        return self._query_flag('SELECT pg_try_advisory_lock(%s, %s) AS flag')

    def _holds_lock(self):
        # Also serves as the connection health check
        return self._query_flag(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND pid = pg_backend_pid() AND classid = %s AND objid = %s) AS flag")

    def _step_down(self):
        self.is_leader = False
        self._close()  # releases the lock if the session is still alive
        try:
            self._on_lose()
        except Exception as e:
            logger.error(f"Failed to stop the scheduler after losing the lock: {e}")

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

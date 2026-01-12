"""
Distributed scheduler with database-backed job storage and leader election.
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional, List

from croniter import croniter

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional['DistributedScheduler'] = None


class DistributedScheduler:
    """
    Database-backed distributed scheduler with leader election.

    In distributed mode:
    - Multiple instances can run simultaneously
    - Only the leader instance executes scheduled jobs
    - Leadership is acquired via database row locking
    - If leader fails, another instance takes over

    In standalone mode (default):
    - Uses APScheduler for backward compatibility
    - No leader election needed
    """

    def __init__(
        self,
        db_backend,
        instance_id: Optional[str] = None,
        mode: str = 'standalone',
        heartbeat_interval: int = 30,
        leadership_timeout: int = 90
    ):
        """
        Initialize the scheduler.

        Args:
            db_backend: Database backend instance
            instance_id: Unique identifier for this instance (auto-generated if not provided)
            mode: 'standalone' (APScheduler) or 'distributed' (database-backed)
            heartbeat_interval: Seconds between heartbeats (distributed mode)
            leadership_timeout: Seconds before leadership expires (distributed mode)
        """
        self.db = db_backend
        self.instance_id = instance_id or str(uuid.uuid4())[:8]
        self.mode = mode
        self.heartbeat_interval = heartbeat_interval
        self.leadership_timeout = leadership_timeout

        self.is_leader = False
        self._running = False
        self._scheduler_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._job_callbacks: Dict[str, Callable] = {}

        # APScheduler for standalone mode
        self._apscheduler = None

        if mode == 'standalone':
            self._init_apscheduler()

        logger.info(f"Scheduler initialized: mode={mode}, instance={self.instance_id}")

    def _init_apscheduler(self):
        """Initialize APScheduler for standalone mode."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._apscheduler = BackgroundScheduler()
            logger.info("APScheduler initialized for standalone mode")
        except ImportError:
            logger.warning("APScheduler not installed, using distributed mode")
            self.mode = 'distributed'

    def start(self):
        """Start the scheduler."""
        if self._running:
            return

        self._running = True

        if self.mode == 'standalone' and self._apscheduler:
            self._apscheduler.start()
            logger.info("Standalone scheduler started")
        else:
            # Start distributed mode threads
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

            self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self._scheduler_thread.start()

            logger.info(f"Distributed scheduler started: instance={self.instance_id}")

    def stop(self):
        """Stop the scheduler."""
        self._running = False

        if self.mode == 'standalone' and self._apscheduler:
            self._apscheduler.shutdown(wait=False)
        else:
            self._release_leadership()

        logger.info("Scheduler stopped")

    def schedule_job(self, job_id: str, cron_expression: str, callback: Callable):
        """
        Schedule a backup job.

        Args:
            job_id: Unique job identifier
            cron_expression: Cron expression for scheduling
            callback: Function to call when job should run
        """
        self._job_callbacks[job_id] = callback

        if self.mode == 'standalone' and self._apscheduler:
            self._schedule_apscheduler_job(job_id, cron_expression, callback)
        else:
            self._schedule_distributed_job(job_id, cron_expression)

    def _schedule_apscheduler_job(self, job_id: str, cron_expression: str, callback: Callable):
        """Schedule job using APScheduler."""
        # Remove existing job if any
        try:
            self._apscheduler.remove_job(job_id)
        except Exception:
            pass

        parts = cron_expression.split()
        if len(parts) == 5:
            self._apscheduler.add_job(
                callback,
                'cron',
                args=[job_id],
                id=job_id,
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                replace_existing=True
            )
            logger.debug(f"APScheduler job scheduled: {job_id} ({cron_expression})")

    def _schedule_distributed_job(self, job_id: str, cron_expression: str):
        """Schedule job in database for distributed mode."""
        next_run = self._calculate_next_run(cron_expression)

        # Upsert into scheduled_jobs table
        existing = self.db.fetchone(
            "SELECT job_id FROM scheduled_jobs WHERE job_id = ?",
            (job_id,)
        )

        if existing:
            self.db.execute(
                "UPDATE scheduled_jobs SET cron_expression = ?, next_run = ?, is_active = 1 WHERE job_id = ?",
                (cron_expression, next_run, job_id)
            )
        else:
            self.db.execute(
                "INSERT INTO scheduled_jobs (job_id, cron_expression, next_run, is_active) VALUES (?, ?, ?, 1)",
                (job_id, cron_expression, next_run)
            )

        self.db.commit()
        logger.debug(f"Distributed job scheduled: {job_id} ({cron_expression}), next_run={next_run}")

    def unschedule_job(self, job_id: str):
        """Remove a scheduled job."""
        self._job_callbacks.pop(job_id, None)

        if self.mode == 'standalone' and self._apscheduler:
            try:
                self._apscheduler.remove_job(job_id)
            except Exception:
                pass
        else:
            self.db.execute(
                "UPDATE scheduled_jobs SET is_active = 0 WHERE job_id = ?",
                (job_id,)
            )
            self.db.commit()

        logger.debug(f"Job unscheduled: {job_id}")

    def _calculate_next_run(self, cron_expression: str) -> str:
        """Calculate next run time from cron expression."""
        try:
            cron = croniter(cron_expression, datetime.now())
            next_run = cron.get_next(datetime)
            return next_run.isoformat()
        except Exception as e:
            logger.error(f"Invalid cron expression '{cron_expression}': {e}")
            # Default to tomorrow at 2 AM
            tomorrow = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=1)
            return tomorrow.isoformat()

    def _heartbeat_loop(self):
        """Send periodic heartbeats and attempt to acquire leadership."""
        while self._running:
            try:
                if self.is_leader:
                    self._update_heartbeat()
                else:
                    self._try_acquire_leadership()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

            time.sleep(self.heartbeat_interval)

    def _scheduler_loop(self):
        """Check and run due jobs (leader only)."""
        while self._running:
            try:
                if self.is_leader:
                    self._run_due_jobs()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            time.sleep(10)  # Check every 10 seconds

    def _try_acquire_leadership(self):
        """Try to become the leader."""
        now = datetime.now().isoformat()
        timeout_threshold = (datetime.now() - timedelta(seconds=self.leadership_timeout)).isoformat()

        # Try to acquire lock if no active leader
        result = self.db.fetchone(
            "SELECT leader_instance, heartbeat_at FROM scheduler_lock WHERE id = 1"
        )

        if result is None:
            # No lock exists, create it
            try:
                self.db.execute(
                    "INSERT INTO scheduler_lock (id, leader_instance, acquired_at, heartbeat_at) VALUES (1, ?, ?, ?)",
                    (self.instance_id, now, now)
                )
                self.db.commit()
                self.is_leader = True
                logger.info(f"Leadership acquired: {self.instance_id}")
            except Exception:
                # Another instance beat us to it
                self.db.rollback()

        elif result['heartbeat_at'] is None or result['heartbeat_at'] < timeout_threshold:
            # Leader timed out, try to take over
            self.db.execute(
                """UPDATE scheduler_lock
                   SET leader_instance = ?, acquired_at = ?, heartbeat_at = ?
                   WHERE id = 1 AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
                (self.instance_id, now, now, timeout_threshold)
            )
            self.db.commit()

            # Check if we got the lock
            result = self.db.fetchone(
                "SELECT leader_instance FROM scheduler_lock WHERE id = 1"
            )
            if result and result['leader_instance'] == self.instance_id:
                self.is_leader = True
                logger.info(f"Leadership acquired (takeover): {self.instance_id}")

    def _update_heartbeat(self):
        """Update the heartbeat timestamp."""
        now = datetime.now().isoformat()
        self.db.execute(
            "UPDATE scheduler_lock SET heartbeat_at = ? WHERE id = 1 AND leader_instance = ?",
            (now, self.instance_id)
        )
        self.db.commit()

        # Verify we still have leadership
        result = self.db.fetchone(
            "SELECT leader_instance FROM scheduler_lock WHERE id = 1"
        )
        if not result or result['leader_instance'] != self.instance_id:
            self.is_leader = False
            logger.warning(f"Lost leadership: {self.instance_id}")

    def _release_leadership(self):
        """Release leadership on shutdown."""
        if self.is_leader:
            self.db.execute(
                "UPDATE scheduler_lock SET leader_instance = NULL, heartbeat_at = NULL WHERE id = 1 AND leader_instance = ?",
                (self.instance_id,)
            )
            self.db.commit()
            self.is_leader = False
            logger.info(f"Leadership released: {self.instance_id}")

    def _run_due_jobs(self):
        """Check and run jobs that are due."""
        now = datetime.now().isoformat()

        # Find due jobs
        due_jobs = self.db.fetchall(
            "SELECT job_id, cron_expression FROM scheduled_jobs WHERE is_active = 1 AND next_run <= ?",
            (now,)
        )

        for job in due_jobs:
            job_id = job['job_id']
            cron_expression = job['cron_expression']

            # Run the job callback
            callback = self._job_callbacks.get(job_id)
            if callback:
                logger.info(f"Running scheduled job: {job_id}")
                try:
                    # Run in separate thread to not block scheduler
                    thread = threading.Thread(target=callback, args=[job_id], daemon=True)
                    thread.start()
                except Exception as e:
                    logger.error(f"Failed to run job {job_id}: {e}")

            # Update next_run and last_run
            next_run = self._calculate_next_run(cron_expression)
            self.db.execute(
                "UPDATE scheduled_jobs SET last_run = ?, next_run = ? WHERE job_id = ?",
                (now, next_run, job_id)
            )
            self.db.commit()

    def get_scheduled_jobs(self) -> List[Dict]:
        """Get all scheduled jobs."""
        if self.mode == 'standalone' and self._apscheduler:
            jobs = []
            for job in self._apscheduler.get_jobs():
                jobs.append({
                    'job_id': job.id,
                    'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
                    'is_active': True
                })
            return jobs
        else:
            return self.db.fetchall(
                "SELECT job_id, cron_expression, next_run, last_run, is_active FROM scheduled_jobs"
            )

    def is_leader_instance(self) -> bool:
        """Check if this instance is the current leader."""
        return self.mode == 'standalone' or self.is_leader


def get_scheduler() -> Optional[DistributedScheduler]:
    """Get the global scheduler instance."""
    return _scheduler


def init_scheduler(db_backend, run_backup_callback: Callable) -> DistributedScheduler:
    """
    Initialize the global scheduler.

    Args:
        db_backend: Database backend instance
        run_backup_callback: Function to call for running backups

    Returns:
        DistributedScheduler instance
    """
    global _scheduler

    mode = os.environ.get('SCHEDULER_MODE', 'standalone').lower()
    instance_id = os.environ.get('INSTANCE_ID')

    _scheduler = DistributedScheduler(
        db_backend=db_backend,
        instance_id=instance_id,
        mode=mode
    )

    # Register the backup callback for all jobs
    _scheduler._default_callback = run_backup_callback

    return _scheduler


def schedule_backup_job(job_id: str, cron_expression: str):
    """
    Schedule a backup job.
    Convenience function that uses the global scheduler.

    Args:
        job_id: Job ID
        cron_expression: Cron expression
    """
    scheduler = get_scheduler()
    if scheduler and scheduler._default_callback:
        scheduler.schedule_job(job_id, cron_expression, scheduler._default_callback)


def unschedule_backup_job(job_id: str):
    """
    Unschedule a backup job.

    Args:
        job_id: Job ID
    """
    scheduler = get_scheduler()
    if scheduler:
        scheduler.unschedule_job(job_id)

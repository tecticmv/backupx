"""
Scheduler module for BackupX.
Supports standalone (APScheduler) and distributed (database-backed) modes.
"""

from .distributed import DistributedScheduler, get_scheduler, init_scheduler

__all__ = ['DistributedScheduler', 'get_scheduler', 'init_scheduler']

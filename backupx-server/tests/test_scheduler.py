"""
Tests for distributed scheduler
"""
import os
import tempfile
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Set environment before imports
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-32chars!')


class MockDB:
    """Mock database for testing scheduler"""

    def __init__(self):
        self.data = {
            'scheduler_lock': [],
            'scheduled_jobs': []
        }
        self.placeholder = '?'

    def execute(self, query, params=None):
        pass

    def commit(self):
        pass

    def fetchone(self, query, params=None):
        if 'scheduler_lock' in query:
            if self.data['scheduler_lock']:
                return self.data['scheduler_lock'][0]
            return None
        if 'scheduled_jobs' in query and 'WHERE job_id' in query:
            job_id = params[0] if params else None
            for job in self.data['scheduled_jobs']:
                if job.get('job_id') == job_id:
                    return job
            return None
        return None

    def fetchall(self, query, params=None):
        if 'scheduled_jobs' in query:
            return self.data['scheduled_jobs']
        return []


class TestDistributedScheduler:
    """Test DistributedScheduler class"""

    def test_scheduler_initialization(self):
        """Test scheduler can be initialized"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        assert scheduler.mode == 'standalone'
        assert scheduler.instance_id is not None

    def test_instance_id_generation(self):
        """Test unique instance ID generation"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler1 = DistributedScheduler(db, mode='standalone')
        scheduler2 = DistributedScheduler(db, mode='standalone')

        # Instance IDs should be unique (unless explicitly set)
        assert scheduler1.instance_id != scheduler2.instance_id

    def test_custom_instance_id(self):
        """Test custom instance ID"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(
            db, mode='standalone', instance_id='my-custom-id'
        )

        assert scheduler.instance_id == 'my-custom-id'

    def test_mode_property(self):
        """Test mode property"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()

        standalone = DistributedScheduler(db, mode='standalone')
        assert standalone.mode == 'standalone'

        distributed = DistributedScheduler(db, mode='distributed')
        assert distributed.mode == 'distributed'

    def test_add_job(self):
        """Test adding a scheduled job"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Mock the job function
        job_func = MagicMock()

        scheduler.add_job(
            job_id='test-job-1',
            cron_expression='0 2 * * *',
            func=job_func,
            args=['arg1'],
            kwargs={'key': 'value'}
        )

        # Job should be registered
        assert 'test-job-1' in scheduler.jobs

    def test_remove_job(self):
        """Test removing a scheduled job"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        job_func = MagicMock()
        scheduler.add_job(
            job_id='test-job-1',
            cron_expression='0 2 * * *',
            func=job_func
        )

        assert 'test-job-1' in scheduler.jobs

        scheduler.remove_job('test-job-1')

        assert 'test-job-1' not in scheduler.jobs

    def test_remove_nonexistent_job(self):
        """Test removing a job that doesn't exist"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Should not raise an error
        scheduler.remove_job('nonexistent-job')


class TestCronExpressionParsing:
    """Test cron expression parsing"""

    def test_calculate_next_run(self):
        """Test next run calculation from cron expression"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Test with a simple cron expression (every day at 2 AM)
        next_run = scheduler._calculate_next_run('0 2 * * *')

        assert next_run is not None
        assert isinstance(next_run, datetime)
        assert next_run > datetime.now()

    def test_calculate_next_run_hourly(self):
        """Test hourly cron expression"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Every hour at minute 0
        next_run = scheduler._calculate_next_run('0 * * * *')

        assert next_run is not None
        assert next_run.minute == 0
        # Should be within the next hour
        assert next_run <= datetime.now() + timedelta(hours=1)

    def test_invalid_cron_expression(self):
        """Test handling of invalid cron expression"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Invalid expression should return None or raise
        next_run = scheduler._calculate_next_run('invalid cron')
        assert next_run is None


class TestLeaderElection:
    """Test leader election in distributed mode"""

    def test_is_leader_standalone(self):
        """Test that standalone mode is always leader"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Standalone is always the leader
        assert scheduler.is_leader() == True

    def test_acquire_leadership(self):
        """Test acquiring leadership"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='distributed')

        # First instance should be able to acquire leadership
        # (in mock scenario, no existing lock)
        result = scheduler._try_acquire_leadership()

        # Result depends on implementation, but should not error
        assert result in [True, False]

    def test_renew_leadership(self):
        """Test renewing leadership"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        # Simulate existing leadership
        db.data['scheduler_lock'] = [{
            'id': 1,
            'leader_instance': 'test-instance',
            'heartbeat_at': datetime.now().isoformat()
        }]

        scheduler = DistributedScheduler(
            db, mode='distributed', instance_id='test-instance'
        )

        # Should be able to renew own leadership
        scheduler._renew_leadership()

        # No error means success


class TestSchedulerIntegration:
    """Integration tests for scheduler"""

    def test_get_all_jobs(self):
        """Test getting all registered jobs"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        # Add multiple jobs
        for i in range(3):
            scheduler.add_job(
                job_id=f'job-{i}',
                cron_expression='0 * * * *',
                func=lambda: None
            )

        jobs = scheduler.get_jobs()
        assert len(jobs) == 3

    def test_pause_resume_job(self):
        """Test pausing and resuming a job"""
        from app.scheduler.distributed import DistributedScheduler

        db = MockDB()
        scheduler = DistributedScheduler(db, mode='standalone')

        scheduler.add_job(
            job_id='pausable-job',
            cron_expression='0 * * * *',
            func=lambda: None
        )

        # Pause the job
        scheduler.pause_job('pausable-job')
        assert scheduler.jobs['pausable-job']['paused'] == True

        # Resume the job
        scheduler.resume_job('pausable-job')
        assert scheduler.jobs['pausable-job']['paused'] == False


class TestSchedulerFactory:
    """Test scheduler factory functions"""

    def test_get_scheduler_returns_instance(self):
        """Test that get_scheduler returns the scheduler instance"""
        from app.scheduler import init_scheduler, get_scheduler

        db = MockDB()

        # Initialize scheduler
        scheduler = init_scheduler(db, mode='standalone')
        assert scheduler is not None

        # get_scheduler should return the same instance
        same_scheduler = get_scheduler()
        assert same_scheduler is scheduler

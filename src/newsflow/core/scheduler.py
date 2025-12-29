"""
Task scheduler for RSS fetching and cleanup.

Uses APScheduler for lightweight async scheduling.
"""

import logging
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from newsflow.config import get_settings

logger = logging.getLogger(__name__)


class TaskScheduler:
    """
    Async task scheduler wrapper.

    Provides simple interface for scheduling periodic tasks.
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._started = False

    def add_job(
        self,
        func: Callable[..., Coroutine[Any, Any, Any]],
        job_id: str,
        interval_minutes: int | None = None,
        interval_hours: int | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Add a periodic job.

        Args:
            func: Async function to run
            job_id: Unique job identifier
            interval_minutes: Run every N minutes
            interval_hours: Run every N hours
            **kwargs: Additional arguments passed to func
        """
        if interval_minutes:
            trigger = IntervalTrigger(minutes=interval_minutes)
        elif interval_hours:
            trigger = IntervalTrigger(hours=interval_hours)
        else:
            raise ValueError("Must specify interval_minutes or interval_hours")

        # Remove existing job if any
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            kwargs=kwargs,
            replace_existing=True,
            max_instances=1,  # Prevent overlapping
            coalesce=True,  # Skip missed runs
        )
        logger.info(f"Scheduled job '{job_id}' with interval {interval_minutes or interval_hours}")

    def reschedule_job(
        self,
        job_id: str,
        interval_minutes: int | None = None,
        interval_hours: int | None = None,
    ) -> bool:
        """
        Reschedule an existing job with new interval.

        Args:
            job_id: Job identifier
            interval_minutes: New interval in minutes
            interval_hours: New interval in hours

        Returns:
            True if job was rescheduled, False if not found
        """
        job = self._scheduler.get_job(job_id)
        if not job:
            return False

        if interval_minutes:
            trigger = IntervalTrigger(minutes=interval_minutes)
        elif interval_hours:
            trigger = IntervalTrigger(hours=interval_hours)
        else:
            return False

        self._scheduler.reschedule_job(job_id, trigger=trigger)
        logger.info(f"Rescheduled job '{job_id}'")
        return True

    def remove_job(self, job_id: str) -> bool:
        """
        Remove a job.

        Args:
            job_id: Job identifier

        Returns:
            True if removed, False if not found
        """
        job = self._scheduler.get_job(job_id)
        if job:
            self._scheduler.remove_job(job_id)
            logger.info(f"Removed job '{job_id}'")
            return True
        return False

    def get_job(self, job_id: str) -> Any:
        """Get job by ID."""
        return self._scheduler.get_job(job_id)

    def start(self) -> None:
        """Start the scheduler."""
        if not self._started:
            self._scheduler.start()
            self._started = True
            logger.info("Scheduler started")

    def shutdown(self, wait: bool = True) -> None:
        """
        Shutdown the scheduler.

        Args:
            wait: Wait for running jobs to complete
        """
        if self._started:
            self._scheduler.shutdown(wait=wait)
            self._started = False
            logger.info("Scheduler shutdown")

    @property
    def running(self) -> bool:
        """Check if scheduler is running."""
        return self._started and self._scheduler.running


# Global scheduler instance
_scheduler: TaskScheduler | None = None


def get_scheduler() -> TaskScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler


def shutdown_scheduler(wait: bool = True) -> None:
    """Shutdown the global scheduler."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=wait)
        _scheduler = None

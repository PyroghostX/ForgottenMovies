import os
from typing import Optional

from filelock import FileLock, Timeout

from forgotten_movies import (
    DATA_DIR,
    flush_log_handlers,
    logger as forgotten_logger,
    mark_job_completed,
    mark_job_started,
    main as run_forgotten_movies_job,
)

JOB_LOCK_PATH = os.path.join(DATA_DIR, "job.lock")
JOB_LOCK_TIMEOUT = float(os.getenv("JOB_LOCK_TIMEOUT", "0.1"))


def execute_job(reason: str) -> None:
    """
    Run the Forgotten Movies workflow and ensure logs are flushed afterward.
    """
    forgotten_logger.info("Forgotten Movies job triggered (%s).", reason)
    mark_job_started(reason)
    try:
        run_forgotten_movies_job()
        forgotten_logger.info("Forgotten Movies job completed (%s).", reason)
        mark_job_completed(reason, "success")
    except Exception:  # pragma: no cover - defensive logging
        forgotten_logger.exception("Forgotten Movies job raised an exception (%s).", reason)
        mark_job_completed(reason, "failed", "See logs for details.")
    finally:
        flush_log_handlers()


def acquire_job_lock(timeout: Optional[float] = JOB_LOCK_TIMEOUT) -> FileLock:
    """
    Acquire and return the inter-process job lock.
    """
    # Use a non-thread-local lock so the holder can release it from worker threads.
    lock = FileLock(JOB_LOCK_PATH, thread_local=False)
    lock.acquire(timeout=timeout)
    return lock


def try_execute_job(reason: str, timeout: Optional[float] = JOB_LOCK_TIMEOUT) -> bool:
    """
    Attempt to execute the job, returning False if the lock could not be acquired.
    """
    lock = FileLock(JOB_LOCK_PATH, thread_local=False)
    try:
        lock.acquire(timeout=timeout)
    except Timeout:
        return False
    try:
        execute_job(reason)
    finally:
        lock.release()
    return True

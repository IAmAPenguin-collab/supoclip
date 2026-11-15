"""Put a video task back in line when its worker dies mid-run.

A running ``process_video_task`` refreshes a heartbeat key in Redis every
30 seconds. When the worker process is killed (a container restart, a crash,
the machine going to sleep), the database still says "processing", the dead
job keeps arq's in-progress lock until its timeout, and the account's single
render slot looks taken. Every other queued video then waits for hours.

The sweep finds processing tasks whose heartbeat has expired, removes the dead
job and its lock, and queues the task again at the front of the account's
line. After ``VIDEO_TASK_AUTO_RECOVERIES`` restarts the task is failed
instead, so a video that crashes the worker every time cannot loop forever.

A clean shutdown leaves the heartbeat to expire on its own: arq re-runs a
cancelled job itself, and that run refreshes the key before the sweep looks.
"""

from __future__ import annotations

import asyncio
import logging
import os

from arq.constants import in_progress_key_prefix, job_key_prefix, retry_key_prefix
from arq.jobs import Job
from sqlalchemy import text

from ..repositories.task_repository import TaskRepository
from .job_queue import DEFAULT_QUEUE_NAME
from .progress import ProgressTracker
from .video_admission import defer_video_job, release_video_slot

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "video_task_heartbeat:{task_id}"
CLAIM_KEY = "video_task_recovering:{task_id}"
HEARTBEAT_EVERY_SECONDS = 30


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def heartbeat_ttl_seconds() -> int:
    """How long a silent task counts as alive. Long enough for a blocked event loop."""
    return _env_int("VIDEO_TASK_HEARTBEAT_TTL_SECONDS", 900, 90)


def max_auto_recoveries() -> int:
    return _env_int("VIDEO_TASK_AUTO_RECOVERIES", 2, 0)


async def _beat(redis, task_id: str, job_id: str) -> None:
    key = HEARTBEAT_KEY.format(task_id=task_id)
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY_SECONDS)
        try:
            await redis.set(key, job_id, ex=heartbeat_ttl_seconds())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not refresh the heartbeat of task %s", task_id, exc_info=True)


async def start_heartbeat(redis, task_id: str, job_id: str) -> asyncio.Task:
    await redis.set(HEARTBEAT_KEY.format(task_id=task_id), job_id, ex=heartbeat_ttl_seconds())
    return asyncio.create_task(_beat(redis, task_id, job_id))


async def stop_heartbeat(beat: asyncio.Task | None, redis, task_id: str, *, clear: bool) -> None:
    """Stop beating. ``clear`` drops the key; a cancelled run keeps it so arq can re-run it."""
    if beat is not None:
        beat.cancel()
        try:
            await beat
        except (asyncio.CancelledError, Exception):
            pass
    if clear:
        try:
            await redis.delete(HEARTBEAT_KEY.format(task_id=task_id))
        except Exception:
            logger.warning("Could not clear the heartbeat of task %s", task_id, exc_info=True)


async def _processing_tasks(db) -> list[tuple[str, str, dict]]:
    rows = await db.execute(
        text(
            """
            SELECT id, user_id, source_metadata FROM tasks
            WHERE status = 'processing'
            ORDER BY created_at ASC
            """
        )
    )
    return [(str(r[0]), str(r[1]), r[2] if isinstance(r[2], dict) else {}) for r in rows]


async def _job_args(redis, job_id: str | None) -> tuple | None:
    if not job_id:
        return None
    try:
        info = await Job(job_id, redis, _queue_name=DEFAULT_QUEUE_NAME).info()
    except Exception:
        logger.warning("Could not read job %s", job_id, exc_info=True)
        return None
    if info is None or info.function != "process_video_task" or not info.args:
        return None
    return tuple(info.args)


async def _drop_dead_job(redis, job_id: str | None) -> None:
    if not job_id:
        return
    await redis.zrem(DEFAULT_QUEUE_NAME, job_id)
    await redis.delete(
        in_progress_key_prefix + job_id, job_key_prefix + job_id, retry_key_prefix + job_id
    )


async def recover_interrupted_tasks(db, redis) -> dict:
    """Queue again every processing task whose worker stopped. Returns counts."""
    requeued, failed = [], []
    for task_id, user_id, metadata in await _processing_tasks(db):
        if await redis.exists(HEARTBEAT_KEY.format(task_id=task_id)):
            continue
        # The startup sweep and the cron sweep can run at the same moment.
        if not await redis.set(CLAIM_KEY.format(task_id=task_id), "1", nx=True, ex=300):
            continue
        job_id = metadata.get("processing_job_id")
        attempts = int(metadata.get("auto_recoveries") or 0)
        args = await _job_args(redis, job_id)
        limit = max_auto_recoveries()
        await _drop_dead_job(redis, job_id)
        await release_video_slot(redis, user_id, task_id)
        progress = ProgressTracker(redis, task_id)

        if args is None or attempts >= limit:
            message = (
                f"The worker stopped during this task {attempts + 1} times. Press Retry to try again."
                if args is not None
                else "The worker stopped during this task. Press Retry to run it again."
            )
            if await TaskRepository.update_task_status(
                db, task_id, "error", progress=0, progress_message=message,
                expected_statuses=["processing"],
            ):
                await progress.update(0, message, "error")
                failed.append(task_id)
            continue

        message = (
            "Restarting automatically: the worker stopped partway through "
            f"(restart {attempts + 1} of {limit})"
        )
        if not await TaskRepository.update_task_status(
            db, task_id, "queued", progress=0, progress_message=message,
            expected_statuses=["processing"],
        ):
            continue
        await TaskRepository.save_source_metadata(db, task_id, {"auto_recoveries": attempts + 1})
        await defer_video_job(db, task_id, args, 0)
        await progress.update(0, message, "queued")
        requeued.append(task_id)

    if requeued or failed:
        logger.warning("Recovered interrupted video tasks: requeued=%s failed=%s", requeued, failed)
    return {"requeued": requeued, "failed": failed}


async def recover_interrupted_video_tasks(ctx) -> dict:
    """Cron entry point."""
    from ..database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        return await recover_interrupted_tasks(db, ctx["redis"])
